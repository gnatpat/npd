import re
from pathlib import Path

from npd.check import SUPPORTED_TOOLS, github_outputs, run_checks
from npd.config import parse_config

BASE = """
[app]
name = "pokemon"

[service]
start = "uv run uvicorn main:app --port $PORT"
"""


def config_with(check: str):
    return parse_config(BASE + check, repo_name="pokemon")


def test_github_outputs_name_each_declared_tool_and_say_there_is_a_check():
    config = config_with('[check]\nsteps = ["true"]\ntools = { node = "24", uv = "latest" }\n')
    assert github_outputs(config) == "check=true\nnode=24\nuv=latest\n"


def test_github_outputs_for_an_app_without_checks_say_so_and_name_no_tools():
    assert github_outputs(parse_config(BASE, repo_name="pokemon")) == "check=false\n"


def test_checks_run_in_order_in_the_check_workdir(tmp_path):
    (tmp_path / "app").mkdir()
    config = config_with(
        '[check]\nworkdir = "app"\nsteps = ["sh -c \'pwd > first\'", "sh -c \'echo two > second\'"]\n'
    )
    assert run_checks(config, tmp_path) == 0
    assert (tmp_path / "app" / "first").read_text().strip() == str(tmp_path / "app")
    assert (tmp_path / "app" / "second").exists()


def test_the_first_failing_check_stops_the_rest_and_its_exit_code_is_returned(tmp_path):
    config = config_with("[check]\nsteps = [\"sh -c 'exit 4'\", \"touch never\"]\n")
    assert run_checks(config, tmp_path) == 4
    assert not (tmp_path / "never").exists()


def test_a_missing_check_command_fails_with_a_message_not_a_traceback(tmp_path, capsys):
    config = config_with('[check]\nsteps = ["no-such-tool-npd --version"]\n')
    assert run_checks(config, tmp_path) != 0
    assert "no-such-tool-npd" in capsys.readouterr().err


def test_an_app_without_checks_passes_and_says_there_was_nothing_to_run(tmp_path, capsys):
    assert run_checks(parse_config(BASE, repo_name="pokemon"), tmp_path) == 0
    assert "no [check]" in capsys.readouterr().out


def test_the_deploy_workflow_sets_up_every_supported_tool():
    """The workflow installs tools from `npd check --github-outputs`. A
    tool npd accepts but the workflow never sets up would let a config pass
    validation and then fail on GitHub with "command not found"."""
    workflow = (Path(__file__).parent.parent / ".github/workflows/deploy.yml").read_text()
    for tool in SUPPORTED_TOOLS:
        assert re.search(rf"if: steps\.tools\.outputs\.{tool}\b", workflow), tool
