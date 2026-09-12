import os
from pathlib import Path

from deploy.paths import app_name_error
from deploy.runner import Runner

GITHUB_USER = os.environ.get("DEPLOY_GITHUB_USER", "gnatpat")


class DirtyRepo(Exception):
    """The working tree has local changes; deploy will not touch it."""


def repo_url(name_or_url: str) -> str:
    """A bare name means the owner's GitHub account; anything with a scheme or
    a colon is already a URL."""
    # Strip trailing slashes to handle tab-completion artifacts
    name_or_url = name_or_url.rstrip("/")

    # Check for URL forms: scheme-style (http://...) or SSH shorthand (user@host:...)
    if "://" in name_or_url or (
        "@" in name_or_url and ":" in name_or_url
    ):
        # This looks like a full URL, pass it through
        return name_or_url

    # Otherwise it should be a bare name; validate it
    error = app_name_error(name_or_url, "repository name")
    if error:
        raise ValueError(error)

    return f"git@github.com:{GITHUB_USER}/{name_or_url}.git"


def clone(url: str, dest: Path, *, runner: Runner) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner.run(["git", "clone", url, str(dest)])


def remote_url(repo: Path, *, runner: Runner) -> str:
    return runner.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"]
    ).stdout.strip()


def head_commit(repo: Path, *, runner: Runner) -> str:
    return runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def is_dirty(repo: Path, *, runner: Runner) -> bool:
    out = runner.run(["git", "-C", str(repo), "status", "--porcelain"]).stdout
    return bool(out.strip())


def pull_ff_only(repo: Path, *, runner: Runner) -> bool:
    """Fast-forward to origin. Returns True if new commits arrived.

    Refuses a dirty tree rather than risking someone's uncommitted work, and
    refuses a non-fast-forward rather than creating a merge nobody asked for.
    """
    if is_dirty(repo, runner=runner):
        raise DirtyRepo(f"{repo} has uncommitted changes; commit or stash first")
    before = head_commit(repo, runner=runner)
    runner.run(["git", "-C", str(repo), "pull", "--ff-only"])
    return head_commit(repo, runner=runner) != before
