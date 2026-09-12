from dataclasses import dataclass
from pathlib import Path

MANAGED_HEADER = "# Managed by deploy — edits will be overwritten"


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

    def unit_file(self, name: str) -> Path:
        return self.units / f"{name}.service"

    def nginx_file(self, name: str) -> Path:
        return self.nginx / f"{name}.conf"

    def env_file(self, name: str) -> Path:
        return self.env / f"{name}.env"

    def clone_dir(self, name: str) -> Path:
        return self.apps / name
