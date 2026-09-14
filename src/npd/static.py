import os
import re
import shutil
from pathlib import Path

from npd.paths import Paths

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]+$")


def publish(source: Path, *, name: str, commit: str, paths: Paths) -> Path:
    """Copy a build output into place and swap the live symlink onto it.

    The swap is a rename, so a reader never sees a half-copied tree: it gets
    either the whole old build or the whole new one.
    """
    if not _COMMIT_PATTERN.match(commit):
        raise ValueError(
            f"commit {commit!r} is not a valid hex string; refusing to build a "
            "path from it"
        )
    if not source.is_dir():
        raise FileNotFoundError(f"build output {source} does not exist")

    # Check that paths.static can exist as a directory
    if paths.static.exists() and not paths.static.is_dir():
        raise ValueError(
            f"{paths.static} is a regular file; must be a directory for static publishing"
        )

    # Check that the live link name is not currently a real directory
    live_path = paths.static / name
    if live_path.exists() and not live_path.is_symlink():
        raise ValueError(
            f"{live_path} is a real directory; move it aside or delete it to publish"
        )

    paths.static.mkdir(parents=True, exist_ok=True)
    previous = live_target(name, paths)
    target = paths.static / f"{name}-{commit}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    # Create the new link under a temp name, then rename it over the old one.
    # os.replace on a symlink is atomic; writing the link in place is not.
    tmp_link = paths.static / f".{name}.swap"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(target.name)
    os.replace(tmp_link, paths.static / name)

    # Keep the new build and the one it replaced (to roll back to by hand);
    # nothing else would ever delete the rest. Republishing the live commit
    # replaced nothing, so it prunes nothing: the older build is still the
    # rollback target.
    if previous != target:
        for old in published_paths(name, paths):
            if old != live_path and old not in (target, previous):
                shutil.rmtree(old)
    return target


def live_target(name: str, paths: Paths) -> Path | None:
    """The versioned directory the live symlink points at, if any."""
    link = paths.static / name
    if not link.is_symlink():
        return None
    return paths.static / os.readlink(link)


def published_paths(name: str, paths: Paths) -> list[Path]:
    """Every path under paths.static this app owns: the live symlink (or
    stray directory of that exact name) plus every versioned build
    directory `publish` left behind for it.

    The live entry is matched by exact name; a build directory is matched
    by the exact `<name>-<hex commit>` shape `publish` creates, not merely a
    `<name>-` prefix -- a bare prefix would also catch a sibling app's own
    live directory (e.g. "boggle" matching "boggle-admin"), which is a
    completely different app, not one of "boggle"'s old builds."""
    if not paths.static.is_dir():
        return []
    owned: list[Path] = []
    live = paths.static / name
    if live.exists() or live.is_symlink():
        owned.append(live)
    build_dir = re.compile(rf"^{re.escape(name)}-[0-9a-f]+$")
    owned += sorted(
        p
        for p in paths.static.iterdir()
        if p.is_dir() and not p.is_symlink() and build_dir.match(p.name)
    )
    return owned


def published_commit(name: str, paths: Paths) -> str | None:
    """The (truncated) commit the live symlink serves, if any."""
    target = live_target(name, paths)
    if target is None:
        return None
    return target.name.removeprefix(f"{name}-")
