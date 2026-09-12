import os
from pathlib import Path

# systemd's EnvironmentFile parser takes bare KEY=VALUE lines and does not
# strip quotes the way a shell would, so values are written unquoted.


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={values[k]}\n" for k in sorted(values))
    path.write_text(body)
    os.chmod(path, 0o600)


def merge_secrets(path: Path, new: dict[str, str]) -> None:
    merged = read_env_file(path)
    merged.update(new)
    write_env_file(path, merged)


def missing_secrets(declared: dict[str, str], existing: dict[str, str]) -> list[str]:
    """Declared secret names with no value on this machine yet."""
    return [name for name in sorted(declared) if name not in existing]
