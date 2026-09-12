import os
import shlex
import subprocess
import threading
from pathlib import Path

from deploy.config import AppConfig
from deploy.proxy import serve
from deploy.runner import Runner
from deploy.secrets import missing_secrets, read_env_file

DEFAULT_DEV_PORT = 8000


class MissingSecrets(Exception):
    def __init__(self, names: list[str], descriptions: dict[str, str]):
        self.names = names
        detail = "\n".join(f"  {n} — {descriptions[n]}" for n in names)
        super().__init__(
            "missing secrets; add them to a gitignored .env in the repo root:\n"
            + detail
        )


def resolve_env(
    config: AppConfig, dotenv: dict[str, str], *, port: int
) -> dict[str, str]:
    """The environment `deploy dev` runs the app with. Fails up front when a
    declared secret has no value, rather than letting the app die confusingly."""
    absent = missing_secrets(config.secrets, dotenv)
    if absent:
        raise MissingSecrets(absent, config.secrets)

    env = dict(os.environ)
    env.update(config.env)
    env.update({name: dotenv[name] for name in config.secrets})
    # Safe to set PORT last and unconditionally: config.py reserves PORT and
    # PATH, so neither [env] nor [secrets] can contain them.
    env["PORT"] = str(port)
    return env


def dev_command(config: AppConfig) -> str:
    if config.is_static:
        raise ValueError("a static app has no start command")
    assert config.service is not None
    return config.dev_start or config.service.start


def dev_workdir(config: AppConfig, repo: Path) -> Path:
    sub = config.service.workdir if config.service else ""
    return repo / sub if sub else repo


def run_dev(
    config: AppConfig,
    repo: Path,
    *,
    port: int = DEFAULT_DEV_PORT,
    build: bool = False,
    prefix: bool = False,
    runner: Runner,
) -> int:
    """Run the app in the foreground. Returns the child's exit code."""
    env = resolve_env(config, read_env_file(repo / ".env"), port=port)

    if build and config.build:
        workdir = repo / config.build.workdir if config.build.workdir else repo
        for step in config.build.steps:
            print(f"[build] {step}")
            runner.run(shlex.split(step), cwd=workdir, env=env)

    if config.is_static:
        output = repo / config.build.output
        print(f"[dev] serving {output} on http://127.0.0.1:{port}")
        return subprocess.call(
            ["python", "-m", "http.server", str(port), "--directory", str(output)]
        )

    app_port = port
    if prefix:
        if config.nginx is None:
            raise ValueError("--prefix needs an [nginx] section with a path")
        app_port = _free_port()
        env["PORT"] = str(app_port)
        threading.Thread(
            target=serve,
            kwargs={
                "nginx": config.nginx,
                "upstream_port": app_port,
                "listen_port": port,
            },
            daemon=True,
        ).start()

    command = dev_command(config)
    print(f"[dev] {command}  (PORT={app_port})")
    return subprocess.call(
        ["/bin/bash", "-c", f"exec {command}"],
        cwd=dev_workdir(config, repo),
        env=env,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
