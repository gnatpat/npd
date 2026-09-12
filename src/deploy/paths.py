import re
from dataclasses import dataclass
from pathlib import Path

MANAGED_HEADER = "# Managed by deploy — edits will be overwritten"

_SAFE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def app_name_error(name: str, what: str = "app name") -> str | None:
    """Why `name` is unsafe as a single path segment, or None if it is fine.
    Returns a message rather than raising so each caller can use its own
    exception type: ConfigError for a bad deploy.toml, ValueError for a bad
    command-line argument.

    This is the one place that knows what makes a name safe to concatenate
    into a path (clone_dir, env_file, systemd_unit_file, ...) — config.py and
    gitrepo.py both used to carry their own copy of this rule; a CLI-supplied
    name needs the same check before it ever reaches Paths."""
    if not name or not name.strip():
        return f"{what} cannot be empty"
    if "/" in name or "\\" in name:
        return (
            f"{what} {name!r} contains slashes; "
            "it must be a single path segment (letters, digits, hyphen, underscore, dot)"
        )
    if name in (".", ".."):
        return (
            f"{what} {name!r} is not allowed; "
            "it must be a single path segment (letters, digits, hyphen, underscore, dot)"
        )
    if not _SAFE_NAME_PATTERN.match(name):
        return (
            f"{what} {name!r} contains invalid characters; "
            "it must contain only letters, digits, hyphen, underscore, or dot"
        )
    return None


@dataclass(frozen=True)
class ArtifactKind:
    """The one place that knows what a generated artifact is called, what it
    is named on disk, and where it lives. Everything downstream (rendering,
    reconciling, reload dispatch) travels with a kind instead of re-deriving
    this from a filename or a hardcoded list.

    `directory_attr` names the `Paths` field this kind lives under (rather
    than a callable) so the dataclass stays plain data — comparable and
    hashable by value, not by lambda identity."""

    label: str  # "systemd unit" — for error messages
    suffix: str  # ".service"
    directory_attr: str  # "units" — the Paths field this kind lives under

    def directory(self, paths: "Paths") -> Path:
        return getattr(paths, self.directory_attr)

    def file(self, paths: "Paths", app_name: str) -> Path:
        return self.directory(paths) / f"{app_name}{self.suffix}"


SYSTEMD_UNIT = ArtifactKind("systemd unit", ".service", "units")
NGINX_SNIPPET = ArtifactKind("nginx snippet", ".conf", "nginx")
ARTIFACT_KINDS = (SYSTEMD_UNIT, NGINX_SNIPPET)


@dataclass(frozen=True)
class Paths:
    """Every location the tool writes to. `under()` exists so tests and
    `--root` can redirect the whole layout into a temp directory."""

    apps: Path
    env: Path
    units: Path
    nginx: Path
    static: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(
            apps=Path.home() / "apps",
            env=Path("/etc/deploy/env"),
            units=Path("/etc/deploy/systemd"),
            nginx=Path("/etc/nginx/deploy.d"),
            static=Path("/var/www/deploy"),
        )

    @classmethod
    def under(cls, root: Path) -> "Paths":
        return cls(
            apps=root / "apps",
            env=root / "etc/deploy/env",
            units=root / "etc/deploy/systemd",
            nginx=root / "etc/nginx/deploy.d",
            static=root / "var/www/deploy",
        )

    def systemd_unit_file(self, name: str) -> Path:
        return SYSTEMD_UNIT.file(self, name)

    def nginx_snippet_file(self, name: str) -> Path:
        return NGINX_SNIPPET.file(self, name)

    def env_file(self, name: str) -> Path:
        return self.env / f"{name}.env"

    def clone_dir(self, name: str) -> Path:
        return self.apps / name
