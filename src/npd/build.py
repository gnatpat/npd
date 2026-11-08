"""Running one build step on a small server that is also running every app.

A build step (npm install, tsc, vite...) can briefly need more memory than a
small box has free. Without care, it pushes the box into thrashing: the apps
stall, sshd stops answering, and the step never finishes. Three things keep
a build from taking the box down, none needing root:

- it runs at a low CPU priority, so the apps stay responsive;
- it volunteers to be the first thing the kernel's OOM killer picks, so
  running out of memory kills the build, not an app or sshd;
- it runs in its own process group under a time limit, so a step that never
  finishes is killed whole (npm -> sh -> tsc ...), and the deploy fails like
  any other failed build.

What this cannot do is cap the build's memory: that needs a cgroup, which an
unprivileged user only gets on a cgroup v2 box (see BACKLOG.md).

Output goes straight to the terminal, never through Runner: the operator
watches a build as it happens (see CLAUDE.md).
"""

import os
import signal
import subprocess
from pathlib import Path

NICENESS = 10
OOM_SCORE_ADJ = Path("/proc/self/oom_score_adj")


def _deprioritise() -> None:
    """Runs in the child between fork and exec. Raising one's own niceness
    and OOM score is allowed without privileges; lowering them is not."""
    os.nice(NICENESS)
    try:
        OOM_SCORE_ADJ.write_text("1000")
    except OSError:
        pass  # not Linux, or /proc is unavailable: the timeout still applies


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_step(argv: list[str], *, cwd: Path, timeout: float) -> int:
    """Run one build step and return its exit code.

    Raises subprocess.TimeoutExpired, after killing the step and everything
    it started, if it runs longer than `timeout` seconds. If npd itself is
    interrupted meanwhile (Ctrl-C), the step is killed too: being in its own
    process group, it would not get the terminal's SIGINT by itself.
    """
    proc = subprocess.Popen(argv, cwd=cwd, process_group=0, preexec_fn=_deprioritise)
    try:
        return proc.wait(timeout=timeout)
    except BaseException:
        _kill_group(proc)
        raise
