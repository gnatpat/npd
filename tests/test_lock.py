import fcntl
import threading

from npd.cli import main
from npd.lock import exclusive
from npd.paths import Paths


def _is_held(lock_file) -> bool:
    with open(lock_file, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def test_a_second_command_waits_for_the_first(tmp_path, capsys):
    lock_file = tmp_path / "apps" / ".npd.lock"
    entered = threading.Event()

    def second():
        with exclusive(lock_file):
            entered.set()

    with exclusive(lock_file):
        thread = threading.Thread(target=second)
        thread.start()
        assert not entered.wait(0.3)
    assert entered.wait(5)
    thread.join()
    assert "waiting for another npd command" in capsys.readouterr().out


def test_update_runs_while_holding_the_lock(tmp_path, monkeypatch):
    held = []
    lock_file = Paths.under(tmp_path).lock_file
    monkeypatch.setattr(
        "npd.cli.commands.update",
        lambda *a, **k: held.append(_is_held(lock_file)) or 0,
    )
    assert main(["--root", str(tmp_path), "update", "crochet"]) == 0
    assert held == [True]


def test_read_only_commands_do_not_take_the_lock(tmp_path, monkeypatch):
    held = []
    lock_file = Paths.under(tmp_path).lock_file
    lock_file.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "npd.cli.commands.list_apps",
        lambda *a, **k: held.append(_is_held(lock_file)) or [],
    )
    assert main(["--root", str(tmp_path), "list"]) == 0
    assert held == [False]
