import fcntl
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def exclusive(lock_file: Path):
    """Hold npd's one lock for the duration: two commands that write config,
    build or restart (a push-triggered deploy and one run by hand, say) must
    not interleave. A second command waits rather than failing. The kernel
    drops the lock when the process exits, however it exits."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("waiting for another npd command to finish…", flush=True)
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
