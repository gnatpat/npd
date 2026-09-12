import difflib
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from npd import settings
from npd.config import AppConfig, ConfigError, parse_config
from npd.gitrepo import clone, head_commit, pull_ff_only, remote_url, repo_url
from npd.health import wait_healthy
from npd.paths import Paths
from npd.ports import allocate_port, ports_in_use
from npd.reconcile import apply_changes, owned_artifacts, plan_app_changes, plan_changes
from npd.render import render
from npd.runner import Runner
from npd.secrets import merge_secrets, missing_secrets, read_env_file
from npd.static import publish, published_paths

SYSTEMCTL = settings.SYSTEMCTL
Prompt = Callable[[str, str], str]


def load_app(name: str, *, paths: Paths, runner: Runner) -> tuple[AppConfig, Path]:
    repo = paths.clone_dir(name)
    config_file = repo / "npd.toml"
    if not config_file.exists():
        raise FileNotFoundError(f"{config_file} does not exist")
    return parse_config(config_file.read_text(), repo_name=name), repo


def installed_apps(paths: Paths) -> list[str]:
    names = {p.stem for p in paths.units.glob("*.service")} if paths.units.is_dir() else set()
    if paths.nginx.is_dir():
        names |= {p.stem for p in paths.nginx.glob("*.conf")}
    return sorted(names)


def _check_route_free(config: AppConfig, *, paths: Paths) -> None:
    """Two apps claiming one route makes nginx reject the whole config, so
    catch it here where the message can name both."""
    if config.nginx is None or not paths.nginx.is_dir():
        return
    for existing in paths.nginx.glob("*.conf"):
        if existing.stem == config.name:
            continue
        if f"location {config.nginx.path} {{" in existing.read_text():
            raise ValueError(
                f"route {config.nginx.path} is already served by {existing.stem}"
            )


def _run_build(config: AppConfig, repo: Path) -> None:
    """Build before anything is rendered, applied, or restarted, so a
    failure leaves the RUNNING SERVICE untouched — the old code keeps
    serving. This does not mean nothing has touched disk: _collect_secrets
    runs before this and may already have written the env file. That is
    deliberate (see _deploy) — a failed build should not force someone to
    retype a password they already entered, and missing_secrets correctly
    skips re-prompting for it on the next run."""
    if not config.build or not config.build.steps:
        return
    workdir = repo / config.build.workdir if config.build.workdir else repo
    for step in config.build.steps:
        print(f"[build] {step}")
        # A build step (npm install, a compiler, ...) can run for minutes;
        # its output is for the operator to watch as it happens, not for
        # this tool to consume -- going through Runner would capture it
        # (RealRunner sets capture_output=True) and throw it away, leaving
        # silence followed by a bare "build step failed" with no diagnosis.
        # Use subprocess.call directly, as logs(), _deploy's journal tail,
        # and run_dev's build loop all do, so the lines reach the terminal,
        # and raise CalledProcessError ourselves so the existing failure
        # handling below still fires.
        returncode = subprocess.call(shlex.split(step), cwd=workdir)
        if returncode != 0:
            print(
                f"{config.name}: build step failed: {step}\n"
                f"  nothing was deployed and the running service is unchanged.\n"
                f"  the working tree in {repo} is now ahead of what is running.",
                file=sys.stderr,
            )
            raise subprocess.CalledProcessError(returncode, step)


def _collect_secrets(config: AppConfig, *, paths: Paths, prompt: Prompt) -> None:
    if not config.secrets:
        return
    env_file = paths.env_file(config.name)
    absent = missing_secrets(config.secrets, read_env_file(env_file))
    if not absent:
        return
    merge_secrets(env_file, {n: prompt(n, config.secrets[n]) for n in absent})


def _tail_journal(name: str) -> None:
    """journalctl's output is for the operator to read right now, not for
    this tool to consume -- going through Runner would capture it
    (RealRunner sets capture_output=True) and throw it away, leaving "FAILED
    health check" on screen with no diagnostics after it. Use
    subprocess.call directly, as logs() does, so the lines reach the
    terminal."""
    subprocess.call(["journalctl", "-u", name, "-n", "20", "--no-pager"])


def _deploy(
    config: AppConfig,
    repo: Path,
    *,
    paths: Paths,
    runner: Runner,
    prompt: Prompt,
) -> int:
    _check_route_free(config, paths=paths)

    port = None
    if not config.is_static:
        assert config.service is not None
        port = allocate_port(paths, name=config.name, pinned=config.service.port)

    _collect_secrets(config, paths=paths, prompt=prompt)
    _run_build(config, repo)

    # There is no state file recording what commit a running process was
    # built from, so it is stamped into the unit itself (see render.py) —
    # this is also what lets commands.update tell a genuine "nothing
    # changed" apart from "the previous build of this same commit failed
    # partway through".
    commit = head_commit(repo, runner=runner)

    if config.is_static:
        assert config.build is not None
        publish(
            repo / config.build.output,
            name=config.name,
            commit=commit[:12],
            paths=paths,
        )

    # plan_app_changes, not plan_changes: it is the only thing that derives
    # what this app previously owned but no longer wants, so a service that
    # becomes static (or drops its [nginx] section) has its stale file removed
    # instead of left on disk holding a port forever.
    changes = plan_app_changes(config.name, render(config, port, paths, commit), paths)
    apply_changes(changes, runner=runner)

    if config.is_static:
        print(f"{config.name}: published")
        return 0

    unit = paths.systemd_unit_file(config.name)

    # link and enable are idempotent -- re-linking/re-enabling an
    # already-correct unit is a no-op -- so they run every time, never
    # gated on whether this is a first install. Gating them was the bug:
    # apply_changes above has already written the unit file by the time we
    # get here, so if `link` fails partway through a first install, a retry
    # sees the unit file already in place, looks like there is nothing left
    # to do, and the old code skipped link/enable/start entirely --
    # registering nothing with systemd while still printing success.
    # check=False on link because its only job here is to make sure the
    # symlink exists; re-linking an already-linked unit is not an error we
    # care about.
    runner.run(["sudo", SYSTEMCTL, "link", str(unit)], check=False)
    runner.run(["sudo", SYSTEMCTL, "enable", config.name])
    # A single restart covers both cases systemctl distinguishes: starting a
    # stopped/never-started unit and restarting a running one. It also runs
    # unconditionally for the same reason link/enable do: _deploy only runs
    # at all once install/update has already decided there is something to
    # do, and the partial-failure retry above must end with the service
    # actually running, not merely registered.
    runner.run(["sudo", SYSTEMCTL, "restart", config.name])

    assert port is not None
    health_path = config.service.health_path if config.service else None
    if not wait_healthy(port, health_path):
        print(f"{config.name}: FAILED health check on port {port}")
        # The service is deliberately left running either way.
        _tail_journal(config.name)
        return 1

    print(f"{config.name}: healthy on port {port}")
    return 0


def _repo_slug(url: str) -> str:
    """The `owner/repo` this URL identifies, lowercased, with a trailing
    `.git` and trailing slash stripped -- so an https origin and an ssh
    origin for the same repo compare equal. ssh shorthand
    (git@host:owner/repo) and https (https://host/owner/repo) both split
    cleanly on '/' and ':', so splitting on either and keeping the last two
    segments works for both forms."""
    url = url.strip().rstrip("/").removesuffix(".git")
    segments = re.split(r"[/:]", url)
    return "/".join(segments[-2:]).lower()


def _existing_clone(url: str, *, paths: Paths, runner: Runner) -> Path | None:
    """Find a clone of `url` already under ~/apps, whatever directory it is
    in. install renames a clone to match [app] name, so on a re-run the
    directory is not necessarily named after the repo any more.

    Compares normalised `owner/repo` slugs, not raw URL strings, so a clone
    whose origin is https (e.g. left over from a manual migration) is still
    recognised against the ssh URL this tool generates -- otherwise a
    second install clones a duplicate that the rename guard below then
    refuses, leaving an orphan. A directory that fails to report an origin
    at all (no `origin` remote configured) is skipped rather than aborting
    install: it is simply not a match, not a reason to fail for an
    unrelated directory under ~/apps.
    """
    if not paths.apps.is_dir():
        return None
    target = _repo_slug(url)
    matches: list[Path] = []
    for d in sorted(paths.apps.iterdir()):
        if not (d / ".git").is_dir():
            continue
        try:
            origin = remote_url(d, runner=runner)
        except subprocess.CalledProcessError:
            continue
        if _repo_slug(origin) == target:
            matches.append(d)
    if len(matches) > 1:
        raise ValueError(
            f"{url} is cloned more than once under {paths.apps}: "
            + ", ".join(str(m) for m in matches)
            + " — remove the one you do not want"
        )
    return matches[0] if matches else None


def install(
    name_or_url: str, *, paths: Paths, runner: Runner, prompt: Prompt
) -> int:
    url = repo_url(name_or_url)
    repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    # A clone already renamed to [app] name (by a previous install) will not
    # be found at ~/apps/<repo_name> any more, so look it up by remote
    # before assuming it needs cloning — otherwise a second install of an
    # app whose name differs from its repo clones a duplicate that is never
    # cleaned up (the hard rule is npd never deletes a clone).
    existing = _existing_clone(url, paths=paths, runner=runner)
    if existing is not None:
        config, repo = load_app(existing.name, paths=paths, runner=runner)
    else:
        repo = paths.clone_dir(repo_name)
        if not repo.exists():
            clone(url, repo, runner=runner)
        config, repo = load_app(repo_name, paths=paths, runner=runner)

    # The repo name and the app name can differ (boggle-solver -> boggle). The
    # clone directory must match the app name, because the unit's
    # WorkingDirectory is derived from it.
    if config.name != repo.name:
        wanted = paths.clone_dir(config.name)
        if wanted.exists():
            raise ValueError(f"{wanted} already exists; cannot rename {repo}")
        repo.rename(wanted)
        repo = wanted

    return _deploy(config, repo, paths=paths, runner=runner, prompt=prompt)


def update(name: str, *, paths: Paths, runner: Runner, prompt: Prompt) -> int:
    config, repo = load_app(name, paths=paths, runner=runner)
    moved = pull_ff_only(repo, runner=runner)
    # Re-read: the pull may have changed npd.toml itself.
    config, repo = load_app(name, paths=paths, runner=runner)

    port = None
    if not config.is_static:
        assert config.service is not None
        port = allocate_port(paths, name=config.name, pinned=config.service.port)

    # A newly declared secret is work to do even when nothing else changed,
    # so it has to be part of the up-to-date test rather than checked after it.
    needs_secret = bool(
        missing_secrets(config.secrets, read_env_file(paths.env_file(config.name)))
    )
    if not moved and not needs_secret:
        # The commit has to be part of this render too, not just _deploy's:
        # it is the only thing that distinguishes "nothing changed" from
        # "the last update pulled these commits but then failed to build
        # them" -- in both cases the working tree's HEAD is the same right
        # now, and the rendered unit would otherwise be byte-identical to
        # what's already on disk even though disk still carries the OLD
        # commit from the build that failed. Only computed in this branch:
        # when moved or needs_secret is already true a deploy is happening
        # regardless, and _deploy computes its own commit for that.
        commit = head_commit(repo, runner=runner)
        if not plan_app_changes(config.name, render(config, port, paths, commit), paths):
            print(f"{name}: already up to date")
            return 0

    return _deploy(config, repo, paths=paths, runner=runner, prompt=prompt)


@dataclass(frozen=True)
class AppStatus:
    name: str
    port: int | None
    route: str | None
    active: str
    commit: str
    behind: int | None = None
    error: str | None = None


def status_of(
    name: str, *, paths: Paths, runner: Runner, fetch: bool = False
) -> AppStatus:
    port = ports_in_use(paths).get(name)
    route = None
    error = None
    config_file = paths.clone_dir(name) / "npd.toml"
    if config_file.exists():
        # A broken npd.toml must not take down the whole `list` command —
        # it is the tool someone reaches for to find out what is wrong, so a
        # bad config for one app is reported on that app's row (route "?")
        # while every other, healthy app still lists normally.
        try:
            config = parse_config(config_file.read_text(), repo_name=name)
        except ConfigError as exc:
            error = str(exc)
        else:
            route = config.nginx.path if config.nginx else None

    active = "unknown"
    if port is not None:
        result = runner.run(
            ["sudo", SYSTEMCTL, "is-active", name], check=False
        )
        active = (result.stdout or "").strip() or "unknown"

    commit = ""
    behind = None
    repo = paths.clone_dir(name)
    if (repo / ".git").exists():
        commit = head_commit(repo, runner=runner)[:8]
        if fetch:
            behind = _commits_behind(repo, runner=runner)

    return AppStatus(
        name=name,
        port=port,
        route=route,
        active=active,
        commit=commit,
        behind=behind,
        error=error,
    )


def _commits_behind(repo: Path, *, runner: Runner) -> int | None:
    """How many commits origin is ahead by. None when there is no upstream."""
    runner.run(["git", "-C", str(repo), "fetch", "--quiet"], check=False)
    result = runner.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD..@{u}"], check=False
    )
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def list_apps(*, paths: Paths, runner: Runner, fetch: bool = False) -> list[AppStatus]:
    return [
        status_of(n, paths=paths, runner=runner, fetch=fetch)
        for n in installed_apps(paths)
    ]


def diff(name: str | None, *, paths: Paths, runner: Runner) -> int:
    """Show what would change. Writes nothing and runs nothing."""
    names = [name] if name else installed_apps(paths)
    any_changes = False
    for app in names:
        config, _ = load_app(app, paths=paths, runner=runner)
        port = None
        if not config.is_static:
            assert config.service is not None
            port = allocate_port(paths, name=app, pinned=config.service.port)
        # Deliberately not passed a commit: diff's contract (see docstring
        # and test_diff_writes_nothing) is that it runs no subprocess at
        # all, and computing the current commit would mean calling out to
        # git. The trade-off is that diff on an already-installed service
        # always shows its NPD_COMMIT line as a pending removal -- a
        # known, accepted limitation, not something this pass fixes.
        for change in plan_app_changes(app, render(config, port, paths), paths):
            any_changes = True
            before = (change.before or "").splitlines(keepends=True)
            after = (change.after or "").splitlines(keepends=True)
            print(
                "".join(
                    difflib.unified_diff(
                        before, after, f"a{change.path}", f"b{change.path}"
                    )
                )
            )
    if not any_changes:
        print("no changes")
    return 0


def restart(name: str, *, paths: Paths, runner: Runner) -> int:
    runner.run(["sudo", SYSTEMCTL, "restart", name])
    config, _ = load_app(name, paths=paths, runner=runner)
    port = ports_in_use(paths).get(name)
    if port is None:
        return 0
    health_path = config.service.health_path if config.service else None
    if wait_healthy(port, health_path):
        return 0
    print(f"{name}: FAILED health check on port {port} after restart")
    _tail_journal(name)
    return 1


def logs(name: str, *, follow: bool) -> int:
    """journalctl is a command the USER watches, not one whose output the
    tool consumes — unlike everything else in this module, it must not go
    through Runner (RealRunner captures stdout/stderr, so `npd logs`
    would print nothing, and `-f` would capture an endless stream forever
    with nothing on screen). subprocess.call inherits the terminal directly,
    the same way dev.run_dev does for the foreground dev server."""
    argv = ["journalctl", "-u", name, "-n", "50"]
    if follow:
        argv.append("-f")
    else:
        argv.append("--no-pager")
    return subprocess.call(argv)  # not runner.run: output is for the user, not the tool


def remove(
    name: str,
    *,
    paths: Paths,
    runner: Runner,
    purge: bool,
    confirm: Callable[[str], bool],
) -> int:
    unit = paths.systemd_unit_file(name)
    clone = paths.clone_dir(name)
    env_file = paths.env_file(name)

    # owned_artifacts derives the set from ARTIFACT_KINDS, so a kind added
    # later is covered here too without editing this function.
    owned = owned_artifacts(name, paths)

    # Nothing anywhere named `name` -- most likely a typo'd app name. Without
    # this, remove() of an app that was never installed silently no-ops
    # (plan_changes/apply_changes have nothing to do) and still prints
    # "removed" and exits 0, making the typo invisible.
    if not owned and not clone.exists() and not env_file.exists():
        print(f"{name}: nothing to remove", file=sys.stderr)
        return 1

    if unit.exists():
        runner.run(["sudo", SYSTEMCTL, "stop", name], check=False)
        runner.run(["sudo", SYSTEMCTL, "disable", name], check=False)

        # Confirm the stop actually took effect before deleting the unit
        # that defines it. If it did not, deleting the unit now would leave
        # a running service invisible to `npd list` (no port record, no
        # unit file) and awkward to kill by hand.
        result = runner.run(["sudo", SYSTEMCTL, "is-active", name], check=False)
        active = (result.stdout or "").strip()
        if active == "active":
            raise ValueError(
                f"{name}: still active after systemctl stop; "
                "stop it by hand before removing"
            )

    changes = plan_changes((), remove=owned)
    apply_changes(changes, runner=runner)

    if purge:
        # published_paths, not a hardcoded pair: a static app also leaves
        # its live symlink and versioned build directories under
        # paths.static, and those are just as much "this app's data" as the
        # clone and env file -- left out, --purge would silently leave them
        # behind forever (there is no other GC for them). published_paths
        # only ever matches this exact name or a "<name>-" prefix, so a
        # differently-named app's files are never at risk.
        targets = [clone, env_file] + published_paths(name, paths)
        existing = [t for t in targets if t.exists() or t.is_symlink()]
        listing = "\n".join(f"  {t}" for t in existing)
        if not existing or not confirm(f"permanently delete:\n{listing}\n"):
            print("keeping the clone, env file, and any published static build")
            return 0
        for target in existing:
            if target.is_symlink() or target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
    print(f"{name}: removed")
    return 0
