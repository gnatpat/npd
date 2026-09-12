import os
from pathlib import Path

# systemd's EnvironmentFile parser takes bare KEY=VALUE lines and does not
# strip quotes the way a shell would, so values are written unquoted.
#
# This module guarantees lossless round-trip through read_env_file and
# write_env_file: values are preserved exactly as written, including trailing
# whitespace. Whether systemd's EnvironmentFile parser itself preserves exotic
# trailing whitespace is a separate concern that has not been verified.


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        # Check stripped version for blank lines and comments, but use
        # original line for value extraction to preserve trailing whitespace.
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={values[k]}\n" for k in sorted(values))
    # Create file with tight permissions from the start to avoid world-readable window.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(body)
    # Ensure permissions are tight even if file already existed.
    os.chmod(path, 0o600)


def merge_secrets(path: Path, new: dict[str, str]) -> None:
    merged = read_env_file(path)
    merged.update(new)
    write_env_file(path, merged)


def missing_secrets(declared: dict[str, str], existing: dict[str, str]) -> list[str]:
    """Declared secret names with no value on this machine yet."""
    return [name for name in sorted(declared) if name not in existing]
