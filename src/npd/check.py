"""`npd check`: run an app's [check] steps (type checks, tests, lints).

Checks run where the code is, never on the server: locally before a push, and
on GitHub before a deploy, where the deploy only goes ahead if they pass. The
server still pulls and builds the app itself; a check only ever says yes or no.

GitHub's machine has none of an app's tools, so [check] names them explicitly
(`tools = { node = "24" }`), and `--github-outputs` hands that list to the
workflow, which runs one setup action per tool.

Output streams straight to the terminal, never through Runner (see CLAUDE.md).
"""

import shlex
import subprocess
import sys
from pathlib import Path

from npd.config import SUPPORTED_TOOLS, AppConfig

__all__ = ["SUPPORTED_TOOLS", "github_outputs", "run_checks"]


def github_outputs(config: AppConfig) -> str:
    """Step outputs for the deploy workflow, one `name=value` line each:
    whether there is a check, and the version of each tool it needs.
    Undeclared tools are left out, which the workflow reads as "skip"."""
    if config.check is None:
        return "check=false\n"
    lines = ["check=true"]
    lines += [f"{tool}={config.check.tools[tool]}" for tool in SUPPORTED_TOOLS if tool in config.check.tools]
    return "\n".join(lines) + "\n"


def run_checks(config: AppConfig, repo: Path) -> int:
    """Run each check step in order; stop at the first failure and return its
    exit code, so a failed type check is the last thing on screen."""
    if config.check is None:
        print("no [check] in npd.toml; nothing to run", flush=True)
        return 0
    workdir = repo / config.check.workdir if config.check.workdir else repo
    for step in config.check.steps:
        print(f"[check] {step}", flush=True)
        try:
            returncode = subprocess.call(shlex.split(step), cwd=workdir)
        except FileNotFoundError:
            print(
                f"{config.name}: check step could not start, command not found: {step}\n"
                f"  on GitHub, list the tool it needs under check.tools",
                file=sys.stderr,
            )
            return 127
        if returncode != 0:
            print(f"{config.name}: check failed: {step}", file=sys.stderr)
            return returncode
    print(f"{config.name}: all checks passed", flush=True)
    return 0
