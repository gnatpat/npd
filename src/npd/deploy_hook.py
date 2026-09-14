"""npd-deploy: the forced command for a deploy key.

A deploy key's authorized_keys line runs this whatever the client asked
for, and the client's request (a bare app name) arrives in
SSH_ORIGINAL_COMMAND. The only thing it can do is `npd update <app>`, which
pulls from the app's own remote, so a leaked key can do no more than
redeploy what is already pushed. See "Deploy on push" in the README.
"""

import os
import re
import sys
from pathlib import Path

from npd import cli

# Deliberately narrower than app_name_error: no leading dot or hyphen, so
# neither "--all" nor a hidden name can slip through as an argument.
_APP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def main() -> int:
    app = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not _APP_NAME.fullmatch(app):
        print(
            "npd-deploy: expected a single app name, e.g. `ssh <server> crochet`",
            file=sys.stderr,
        )
        return 2
    # sshd gives a forced command a minimal PATH; build steps may need uv.
    local_bin = Path.home() / ".local" / "bin"
    os.environ["PATH"] = f"{local_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    return cli.main(["update", app])
