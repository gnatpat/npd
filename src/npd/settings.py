"""Everything that is specific to one machine or one person, in one place.

Every value can be overridden with an environment variable. The defaults are
chosen so that running npd as the user who owns the apps needs no
configuration at all.

Known caveat: SERVICE_USER and the clone location (Paths.default(), under
Path.home()) must agree, because the generated unit's `User=` line runs the
app against a WorkingDirectory derived from that same home. Defaulting
SERVICE_USER to the invoking user keeps those consistent automatically. If
NPD_USER is set to a *different* user than the one running npd, the unit
will run as that user against clones cloned into the invoking user's home --
this module does not try to reconcile that, it is on whoever sets NPD_USER
to also make sure the clones live where that user expects them.
"""

import getpass
import os
from pathlib import Path

# The user the generated units run as, and whose home holds the clones.
# Defaults to whoever is running npd, which is almost always right and is
# what makes the tool work unmodified on someone else's machine.
SERVICE_USER = os.environ.get("NPD_USER") or getpass.getuser()

# The PATH the generated units use. Explicit rather than a login shell, so a
# unit never depends on a shell profile to locate `uv`.
UNIT_PATH = os.environ.get("NPD_UNIT_PATH") or f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"

GITHUB_USER = os.environ.get("NPD_GITHUB_USER", "gnatpat")
SYSTEMCTL = os.environ.get("NPD_SYSTEMCTL", "/usr/bin/systemctl")
PORT_RANGE_START = int(os.environ.get("NPD_PORT_RANGE_START", "8200"))
PORT_RANGE_END = int(os.environ.get("NPD_PORT_RANGE_END", "8300"))  # exclusive

# The on-disk layout. Paths.default() and Paths.under() (paths.py) are the
# only things that read these; a temp-directory root passed via --root or a
# test never touches these at all.
ETC_ENV_DIR = Path(os.environ.get("NPD_ETC_ENV_DIR", "/etc/npd/env"))
ETC_SYSTEMD_DIR = Path(os.environ.get("NPD_ETC_SYSTEMD_DIR", "/etc/npd/systemd"))
NGINX_SNIPPET_DIR = Path(os.environ.get("NPD_NGINX_SNIPPET_DIR", "/etc/nginx/npd.d"))
STATIC_DIR = Path(os.environ.get("NPD_STATIC_DIR", "/var/www/npd"))
