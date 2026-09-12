import difflib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deploy.config import AppConfig, ConfigError, parse_config
from deploy.gitrepo import clone, head_commit, pull_ff_only, remote_url, repo_url
from deploy.health import wait_healthy
from deploy.paths import SYSTEMD_UNIT, Paths
from deploy.ports import allocate_port, ports_in_use
from deploy.reconcile import apply_changes, owned_artifacts, plan_app_changes, plan_changes
from deploy.render import render
from deploy.runner import Runner
from deploy.secrets import merge_secrets, missing_secrets, read_env_file
from deploy.static import publish

SYSTEMCTL = "/usr/bin/systemctl"
Prompt = Callable[[str, str], str]


def load_app(name: str, *, paths: Paths, runner: Runner) -> tuple[AppConfig, Path]:
    repo = paths.clone_dir(name)
    config_file = repo / "deploy.toml"
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


def _run_build(config: AppConfig, repo: Path, *, runner: Runner) -> None:
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
        try:
            runner.run(shlex.split(step), cwd=workdir)
        except subprocess.CalledProcessError:
            print(
                f"{config.name}: build step failed: {step}\n"
                f"  nothing was deployed and the running service is unchanged.\n"
                f"  the working tree in {repo} is now ahead of what is running.",
                file=sys.stderr,
            )
            raise


def _collect_secrets(config: AppConfig, *, paths: Paths, prompt: Prompt) -> None:
    if not config.secrets:
        return
    env_file = paths.env_file(config.name)
    absent = missing_secrets(config.secrets, read_env_file(env_file))
    if not absent:
        return
    merge_secrets(env_file, {n: prompt(n, config.secrets[n]) for n in absent})


def _deploy(
    config: AppConfig,
    repo: Path,
    *,
    paths: Paths,
    runner: Runner,
    prompt: Prompt,
    first_install: bool,
    code_changed: bool = False,
) -> int:
    _check_route_free(config, paths=paths)

    port = None
    if not config.is_static:
        assert config.service is not None
        port = allocate_port(paths, name=config.name, pinned=config.service.port)

    _collect_secrets(config, paths=paths, prompt=prompt)
    _run_build(config, repo, runner=runner)

    if config.is_static:
        assert config.build is not None
        publish(
            repo / config.build.output,
            name=config.name,
            commit=head_commit(repo, runner=runner)[:12],
            paths=paths,
        )

    # plan_app_changes, not plan_changes: it is the only thing that derives
    # what this app previously owned but no longer wants, so a service that
    # becomes static (or drops its [nginx] section) has its stale file removed
    # instead of left on disk holding a port forever.
    changes = plan_app_changes(config.name, render(config, port, paths), paths)
    apply_changes(changes, runner=runner)

    if config.is_static:
        print(f"{config.name}: published")
        return 0

    unit_changed = any(c.kind is SYSTEMD_UNIT for c in changes)
    unit = paths.systemd_unit_file(config.name)

    if first_install:
        runner.run(["sudo", SYSTEMCTL, "link", str(unit)])
        runner.run(["sudo", SYSTEMCTL, "enable", config.name])
        runner.run(["sudo", SYSTEMCTL, "start", config.name])
    elif unit_changed or code_changed:
        # New commits matter even when the unit is byte-identical: the running
        # process is still executing the old code until it is restarted.
        runner.run(["sudo", SYSTEMCTL, "restart", config.name])

    assert port is not None
    health_path = config.service.health_path if config.service else None
    if not wait_healthy(port, health_path):
        print(f"{config.name}: FAILED health check on port {port}")
        # journalctl's output is for the operator to read right now, not for
        # this tool to consume -- going through Runner would capture it
        # (RealRunner sets capture_output=True) and throw it away, leaving
        # "FAILED health check" on screen with no diagnostics after it. Use
        # subprocess.call directly, as logs() does, so the lines reach the
        # terminal. The service is deliberately left running either way.
        subprocess.call(["journalctl", "-u", config.name, "-n", "20", "--no-pager"])
        return 1

    print(f"{config.name}: healthy on port {port}")
    return 0


def _existing_clone(url: str, *, paths: Paths, runner: Runner) -> Path | None:
    """Find a clone of `url` already under ~/apps, whatever directory it is
    in. install renames a clone to match [app] name, so on a re-run the
    directory is not necessarily named after the repo any more."""
    if not paths.apps.is_dir():
        return None
    matches = [
        d
        for d in sorted(paths.apps.iterdir())
        if (d / ".git").is_dir() and remote_url(d, runner=runner) == url
    ]
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
    # cleaned up (the hard rule is deploy never deletes a clone).
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

    first = not paths.systemd_unit_file(config.name).exists()
    return _deploy(
        config, repo, paths=paths, runner=runner, prompt=prompt, first_install=first
    )


def update(name: str, *, paths: Paths, runner: Runner, prompt: Prompt) -> int:
    config, repo = load_app(name, paths=paths, runner=runner)
    moved = pull_ff_only(repo, runner=runner)
    # Re-read: the pull may have changed deploy.toml itself.
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
    if (
        not moved
        and not needs_secret
        and not plan_app_changes(config.name, render(config, port, paths), paths)
    ):
        print(f"{name}: already up to date")
        return 0

    return _deploy(
        config,
        repo,
        paths=paths,
        runner=runner,
        prompt=prompt,
        first_install=False,
        code_changed=moved,
    )


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
    config_file = paths.clone_dir(name) / "deploy.toml"
    if config_file.exists():
        # A broken deploy.toml must not take down the whole `list` command —
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
    return 1


def logs(name: str, *, follow: bool) -> int:
    """journalctl is a command the USER watches, not one whose output the
    tool consumes — unlike everything else in this module, it must not go
    through Runner (RealRunner captures stdout/stderr, so `deploy logs`
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
    if unit.exists():
        runner.run(["sudo", SYSTEMCTL, "stop", name], check=False)
        runner.run(["sudo", SYSTEMCTL, "disable", name], check=False)

        # Confirm the stop actually took effect before deleting the unit
        # that defines it. If it did not, deleting the unit now would leave
        # a running service invisible to `deploy list` (no port record, no
        # unit file) and awkward to kill by hand.
        result = runner.run(["sudo", SYSTEMCTL, "is-active", name], check=False)
        active = (result.stdout or "").strip()
        if active == "active":
            raise ValueError(
                f"{name}: still active after systemctl stop; "
                "stop it by hand before removing"
            )

    # owned_artifacts derives the set from ARTIFACT_KINDS, so a kind added
    # later is removed here too without editing this function.
    changes = plan_changes((), remove=owned_artifacts(name, paths))
    apply_changes(changes, runner=runner)

    if purge:
        targets = [paths.clone_dir(name), paths.env_file(name)]
        existing = [t for t in targets if t.exists()]
        listing = "\n".join(f"  {t}" for t in existing)
        if not existing or not confirm(f"permanently delete:\n{listing}\n"):
            print("keeping the clone and env file")
            return 0
        for target in existing:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    print(f"{name}: removed")
    return 0
