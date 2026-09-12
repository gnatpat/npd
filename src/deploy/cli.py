import argparse
import getpass
import sys
from pathlib import Path

from deploy import commands
from deploy.config import ConfigError, parse_config
from deploy.dev import DEFAULT_DEV_PORT, run_dev
from deploy.gitrepo import DirtyRepo
from deploy.paths import Paths, app_name_error
from deploy.reconcile import ApplyFailed, ForeignFile
from deploy.runner import RealRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument(
        "--root",
        type=Path,
        help="redirect the whole on-disk layout under this directory (testing)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="clone, build, wire up and start an app")
    p.add_argument("name")

    p = sub.add_parser("update", help="pull, build, apply changes and restart")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("list", help="show installed apps")
    p.add_argument("--fetch", action="store_true", help="also report commits behind")

    p = sub.add_parser("diff", help="show what would change; writes nothing")
    p.add_argument("name", nargs="?")

    p = sub.add_parser("restart")
    p.add_argument("name")

    p = sub.add_parser("logs")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")

    p = sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="also delete clone and secrets")

    p = sub.add_parser("dev", help="run this repo locally")
    p.add_argument("--prefix", action="store_true", help="serve under the nginx route")
    p.add_argument("--build", action="store_true", help="run build steps first")
    p.add_argument("--port", type=int, default=DEFAULT_DEV_PORT)

    return parser


def _prompt(name: str, description: str) -> str:
    return getpass.getpass(f"{name} ({description}): ")


def _confirm(message: str) -> bool:
    return input(f"{message}type 'yes' to confirm: ").strip() == "yes"


def _validated(name: str) -> str:
    """Reject a CLI-supplied app name before it ever reaches Paths.

    Without this, a name straight from argparse flows unchecked into
    Paths.clone_dir / env_file, which just concatenate — so e.g. `deploy
    remove --purge "../../something"` could construct a path outside
    ~/apps. Raises ValueError, which main() already catches and prints
    cleanly."""
    error = app_name_error(name)
    if error:
        raise ValueError(error)
    return name


def _run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.under(args.root) if args.root else Paths.default()
    runner = RealRunner()

    if args.command == "install":
        # install's argument is a bare app name OR a full git URL
        # (repo_url tells them apart); it is not a lone path segment like
        # the other commands' `name`, so it goes through repo_url's own
        # validation instead of _validated here.
        return commands.install(
            args.name, paths=paths, runner=runner, prompt=_prompt
        )

    if args.command == "update":
        names = (
            commands.installed_apps(paths) if args.all else [args.name]
        )
        if not names or names == [None]:
            print("give an app name or --all", file=sys.stderr)
            return 2
        if not args.all:
            _validated(args.name)
        return max(
            commands.update(n, paths=paths, runner=runner, prompt=_prompt)
            for n in names
        )

    if args.command == "list":
        for app in commands.list_apps(paths=paths, runner=runner, fetch=args.fetch):
            port = app.port if app.port is not None else "-"
            behind = "" if app.behind in (None, 0) else f"  ({app.behind} behind)"
            line = (
                f"{app.name:<12} {str(port):<6} {app.route or '-':<14} "
                f"{app.active:<10} {app.commit}{behind}"
            )
            if app.error:
                line += f"  [error: {app.error}]"
            print(line)
        return 0

    if args.command == "diff":
        if args.name is not None:
            _validated(args.name)
        return commands.diff(args.name, paths=paths, runner=runner)

    if args.command == "restart":
        return commands.restart(_validated(args.name), paths=paths, runner=runner)

    if args.command == "logs":
        return commands.logs(_validated(args.name), follow=args.follow)

    if args.command == "remove":
        return commands.remove(
            _validated(args.name),
            paths=paths,
            runner=runner,
            purge=args.purge,
            confirm=_confirm,
        )

    if args.command == "dev":
        repo = Path.cwd()
        config = parse_config(
            (repo / "deploy.toml").read_text(), repo_name=repo.name
        )
        return run_dev(
            config,
            repo,
            port=args.port,
            build=args.build,
            prefix=args.prefix,
            runner=runner,
        )

    return 2


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ApplyFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.actions_completed:
            # The failure happened partway. Say what already took effect, so
            # the user knows whether the box is in the old state or a mixed one.
            done = ", ".join(sorted(exc.actions_completed))
            print(f"  these already took effect: {done}", file=sys.stderr)
        if exc.unrestored:
            stuck = ", ".join(str(p) for p in exc.unrestored)
            print(f"  could not roll back: {stuck} — check by hand", file=sys.stderr)
        return 1
    except (ConfigError, ForeignFile, DirtyRepo, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
