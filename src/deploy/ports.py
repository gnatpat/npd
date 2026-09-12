import re
import sys

from deploy.paths import Paths

PORT_RANGE = range(8200, 8300)
_PORT_LINE = re.compile(r'Environment="?PORT=(\d+)"?', re.MULTILINE)


class PortExhausted(Exception):
    """Every port in PORT_RANGE is taken."""


def ports_in_use(paths: Paths, *, exclude: str | None = None) -> dict[str, int]:
    """App name -> port, read back out of the generated units.

    The units are the only record of port assignments; there is no state file
    that could disagree with them.
    """
    found: dict[str, int] = {}
    if not paths.units.is_dir():
        return found
    for unit in sorted(paths.units.glob("*.service")):
        name = unit.stem
        if name == exclude:
            continue
        try:
            text = unit.read_text()
        except (OSError, UnicodeDecodeError) as e:
            print(f"Warning: skipping unreadable unit {unit.name}: {e}", file=sys.stderr)
            continue
        match = _PORT_LINE.search(text)
        if match:
            found[name] = int(match.group(1))
    return found


def allocate_port(paths: Paths, *, name: str, pinned: int | None = None) -> int:
    """Pick this app's port. An app always keeps the port it already has."""
    taken = ports_in_use(paths, exclude=name)
    mine = ports_in_use(paths).get(name)

    if pinned is not None:
        if pinned in taken.values():
            owner = next(n for n, p in taken.items() if p == pinned)
            raise ValueError(f"port {pinned} is already used by {owner}")
        return pinned

    if mine is not None:
        return mine

    used = set(taken.values())
    for port in PORT_RANGE:
        if port not in used:
            return port
    raise PortExhausted(
        f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}"
    )
