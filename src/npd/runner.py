import subprocess
from pathlib import Path
from typing import Protocol, Sequence


class Runner(Protocol):
    """Every subprocess the tool makes goes through this, so tests can record
    commands instead of running them."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess: ...


class RealRunner:
    def run(self, argv, *, cwd=None, env=None, check=True):
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )


class RecordingRunner:
    """Test double. `results` maps a substring of the joined command to the exit
    code it should return; `stdout` maps a substring to the output it should
    produce. Anything unmatched succeeds with empty output."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        stdout: dict[str, str] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.results = results or {}
        self.stdout = stdout or {}

    def run(self, argv, *, cwd=None, env=None, check=True):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        code = next(
            (c for frag, c in self.results.items() if frag in joined), 0
        )
        out = next((o for frag, o in self.stdout.items() if frag in joined), "")
        result = subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        if check and code != 0:
            raise subprocess.CalledProcessError(code, argv, output="", stderr="")
        return result

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)
