import os
import re
import shutil
from pathlib import Path

from deploy.paths import Paths

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
    return target


def live_target(name: str, paths: Paths) -> Path | None:
    """The versioned directory the live symlink points at, if any."""
    link = paths.static / name
    if not link.is_symlink():
        return None
    return paths.static / os.readlink(link)
