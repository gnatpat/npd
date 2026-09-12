from dataclasses import dataclass
from pathlib import Path

MANAGED_HEADER = "# Managed by deploy — edits will be overwritten"


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
