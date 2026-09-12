import difflib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deploy.config import AppConfig, parse_config
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
        runner.run(
            ["journalctl", "-u", config.name, "-n", "20", "--no-pager"], check=False
        )
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
