import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from npd.build import run_step


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of ours is a zombie until reaped; that counts as gone.
    status = Path(f"/proc/{pid}/status")
    return not (status.exists() and "\nState:\tZ" in status.read_text())


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_a_step_returns_its_exit_code(tmp_path):
    assert run_step(["true"], cwd=tmp_path, timeout=30) == 0
    assert run_step(["sh", "-c", "exit 3"], cwd=tmp_path, timeout=30) == 3


def test_a_step_runs_in_the_build_workdir(tmp_path):
    run_step(["sh", "-c", "pwd > where"], cwd=tmp_path, timeout=30)
    assert (tmp_path / "where").read_text().strip() == str(tmp_path)


def test_a_step_runs_at_a_lower_cpu_priority_than_the_apps(tmp_path):
    """Builds share the box with every running app; a build must not
    starve them of CPU while it runs."""
    run_step(["sh", "-c", "nice > niceness"], cwd=tmp_path, timeout=30)
    expected = min(os.nice(0) + 10, 19)
    assert int((tmp_path / "niceness").read_text()) == expected


@pytest.mark.skipif(not Path("/proc/self/oom_score_adj").exists(), reason="Linux only")
def test_a_step_is_the_first_thing_the_kernel_kills_when_memory_runs_out(tmp_path):
    """On a small box with no hard memory limit, the OOM killer must pick
    the build, not a running app or sshd."""
    run_step(["sh", "-c", "cat /proc/self/oom_score_adj > score"], cwd=tmp_path, timeout=30)
    assert (tmp_path / "score").read_text().strip() == "1000"


def test_a_step_that_runs_too_long_is_killed_with_everything_it_started(tmp_path):
    """A build thrashing a box out of memory never finishes. The timeout
    must kill the whole step (npm run build -> sh -> tsc ...), not just the
    direct child, or the grandchildren keep the box swamped."""
    script = "sleep 60 & echo $! > child; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        run_step(["sh", "-c", script], cwd=tmp_path, timeout=1)
    child = int((tmp_path / "child").read_text())
    assert _wait_gone(child)


def test_an_interrupted_deploy_kills_the_running_step(tmp_path):
    """The step runs in its own process group (so the timeout can kill it
    whole), which also means Ctrl-C at the terminal no longer reaches it.
    Interrupting npd must take the step down with it rather than leave it
    running unattended."""
    script = "sleep 60 & echo $! > child; wait"
    started = threading.Event()

    def interrupt():
        while not (tmp_path / "child").exists():
            time.sleep(0.02)
        started.set()
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        run_step(["sh", "-c", script], cwd=tmp_path, timeout=30)
    assert started.is_set()
    child = int((tmp_path / "child").read_text())
    assert _wait_gone(child)
