import pytest

from npd import deploy_hook


@pytest.fixture
def npd_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(deploy_hook.cli, "main", lambda argv: calls.append(argv) or 0)
    return calls


def test_an_app_name_becomes_npd_update(monkeypatch, npd_calls):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "crochet")
    assert deploy_hook.main() == 0
    assert npd_calls == [["update", "crochet"]]


def test_the_update_exit_code_is_passed_back(monkeypatch):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "crochet")
    monkeypatch.setattr(deploy_hook.cli, "main", lambda argv: 1)
    assert deploy_hook.main() == 1


@pytest.mark.parametrize(
    "request_",
    ["", "--all", "crochet --all", "crochet\n", "../crochet", "crochet; reboot", "-h"],
)
def test_anything_but_one_app_name_is_refused(monkeypatch, capsys, npd_calls, request_):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", request_)
    assert deploy_hook.main() == 2
    assert npd_calls == []
    assert "expected a single app name" in capsys.readouterr().err


def test_no_command_at_all_is_refused(monkeypatch, npd_calls):
    monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
    assert deploy_hook.main() == 2
    assert npd_calls == []


def test_local_bin_is_on_path_for_build_steps(monkeypatch, tmp_path, npd_calls):
    # sshd gives a forced command a minimal PATH, and uv lives in ~/.local/bin.
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "crochet")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    deploy_hook.main()
    import os

    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path / ".local" / "bin")
