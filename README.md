# npd

Deploy small web apps to one server without hand-writing systemd units and
nginx config every time.

`npd` reads a short `npd.toml` from a repository and generates everything the
server needs to run it: a systemd unit, an nginx `location` block, a port, and
a place to keep secrets. Installing an app is one command. So is updating it.

```console
$ npd install pokemon
$ npd list
boggle       -      /boggle/       published     3abb3c25
pokemon      8201   /pokemon/      active        a3f91c2
$ npd update pokemon
pokemon: already up to date
```

## Is this for you?

It is worth being honest about the scope. `npd` is deliberately small and
suits one particular situation:

- **One server**, with systemd and nginx already on it, serving several small
  apps under path prefixes on a single domain.
- **Apps you own**, deployed from git, that each run as a single long-lived
  process — or that build to static files.
- **You are comfortable with systemd and nginx** and want less typing, not an
  abstraction over them. `npd` generates config you can read, and puts it
  where you would have put it yourself.

If you want zero-downtime deploys, multiple servers, containers, rollbacks or
a web UI, use Dokku, Kamal or Coolify. They are better at those things and
`npd` does not try.

## How it works

Three layers, and the middle one is the interesting part:

```
npd.toml ──parse──► AppConfig ──render──► exact file contents ──reconcile──► disk + reloads
```

Rendering is a pure function: given a config and a port it returns the precise
text of every file the app owns. Reconciling diffs that against what is on
disk, writes only what differs, and runs only the reloads those changes
require. Nothing else decides what gets written.

Two consequences worth knowing:

**`npd diff` is free and safe.** It runs the render, diffs it, prints the
result and stops. No writes, no subprocesses. Run it before trusting anything.

**There is no state file.** The generated systemd unit *is* the record. Its
`Environment="PORT=…"` line is where the port assignment lives, its
`NPD_COMMIT` line is the commit the running process was started from, and the
set of installed apps is the set of generated files. Derived state cannot go
stale, and there is no registry that can disagree with reality.

## Installing

Requires Python 3.11+ (for `tomllib`). No runtime dependencies.

```console
$ uv tool install git+ssh://git@github.com/gnatpat/npd
```

Then, once per server:

1. Create the directories `npd` writes to, owned by the user your apps run as:

   ```console
   $ sudo mkdir -p /etc/npd/env /etc/npd/systemd /etc/nginx/npd.d /var/www/npd
   $ sudo chown -R "$USER:$USER" /etc/npd /etc/nginx/npd.d /var/www/npd
   $ sudo chmod 700 /etc/npd/env
   ```

2. Add this line inside the `server { … }` block of the site you are adding
   apps to, then reload nginx:

   ```nginx
   include /etc/nginx/npd.d/*.conf;
   ```

   `npd` never edits that file again. Your existing config — static roots,
   error pages, TLS — is left alone, and each app drops a file into
   `npd.d/` instead.

3. Allow the two privileged operations without a password, in
   `/etc/sudoers.d/npd`:

   ```
   <user> ALL=(ALL) NOPASSWD: /usr/bin/systemctl
   <user> ALL=(ALL) NOPASSWD: /usr/sbin/nginx -t
   <user> ALL=(ALL) NOPASSWD: /usr/sbin/nginx -s reload
   ```

   These are the only things `npd` uses `sudo` for. Everything else — git,
   builds, writing config — runs as you.

   Be aware that passwordless `systemctl` is as good as root: anyone with a
   shell as this user can start any command as root through a unit. Never hand
   an unrestricted SSH key for this user to CI; see [Deploy on push](#deploy-on-push).

## `npd.toml`

One file at the root of each repository you deploy.

```toml
[app]
name = "pokemon"            # optional, defaults to the repository name
type = "service"            # "service" (default) or "static"

[build]                     # omit if there is nothing to build
workdir = "frontend"
steps = ["npm install", "npm run build"]

[service]
workdir = "server"          # where the start command runs, relative to the repo
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
health_path = "/"           # optional; must return 2xx after a restart
port = 8151                 # optional; pin a port instead of being assigned one
sandbox = false             # optional; see "Sandboxing" below

[nginx]                     # omit for a service with no public route
path = "/pokemon/"
strip_prefix = true         # see below — this one matters
client_max_body_size = "10m"

[dev]                       # optional; overrides [service] for `npd dev`
start = "uv run uvicorn main:app --reload --port $PORT"

[env]                       # non-secret, committed
LOG_LEVEL = "info"

[secrets]                   # names and descriptions only, never values
API_TOKEN = "token for the upload endpoint"
```

An absent section means "do not do that": no `[build]`, no build step; no
`[nginx]`, no public route. Any key or table not shown above is an error, so a
typo fails loudly instead of being ignored.

`$PORT` is always present in the environment. `npd` assigns one from 8200–8299
unless you pin it, reads it back out of the generated unit, and never hands
the same port to two apps.

### `strip_prefix`

This decides one character — the trailing slash on nginx's `proxy_pass` — and
it changes what your app receives:

| `strip_prefix` | a request to `/pokemon/cards` reaches the app as |
|---|---|
| `true` (default) | `/cards` |
| `false` | `/pokemon/cards` |

`X-Forwarded-Prefix` is sent either way, so an app that strips can still build
correct absolute URLs. Getting this backwards breaks every route in the app,
so if you are adopting an app that already works, copy whatever its current
nginx config does rather than guessing — and check it locally first with
`npd dev --prefix`.

### Secrets

`[secrets]` declares names and what they are for, never values. `npd install`
prompts for anything missing and writes `/etc/npd/env/<app>.env` at mode 0600,
which the unit loads with `EnvironmentFile=`. Adding a secret to a repo means
the next `npd update` prompts for it — you do not have to remember to go and
edit a file on the server.

`npd secret set <app> <NAME>` stores a value without waiting to be asked; with
`--stdin` it reads the value from a pipe, for `ssh server npd secret set …`.
Use it *before* pushing the commit that declares the secret — a deploy triggered
from CI has no terminal to prompt at, so it would otherwise fail — and to
rotate a value later, which restarts the app if it is already loading secrets.

Locally, `npd dev` reads the same names from a gitignored `.env` in the repo
root and tells you which are missing before it starts anything.

### Sandboxing

`sandbox = true` adds a fixed set of systemd hardening lines to the unit. The
app sees an empty `/home` apart from its own clone (which stays writable),
cannot read any app's env file (its own secrets still arrive, because systemd
reads `EnvironmentFile=` first), cannot write anywhere else, cannot gain
privileges, and is capped at 200 MB of memory, 100 processes and half a CPU.

It is meant for small, untrusted-feeling code. Apps whose toolchain lives in
your home directory — `uv run`, nvm's `node` — will not start with it on,
because the sandbox hides that too.

### Static apps

```toml
[app]
name = "boggle"
type = "static"

[build]
steps = ["uv run python make_static.py"]
output = "static"           # directory the build produces

[nginx]
path = "/boggle/"
```

No process, no port, no unit. Builds are published to
`/var/www/npd/<name>-<commit>` and the live symlink is swapped atomically, so
a rebuild never serves a half-copied tree. The build it replaced stays on disk
(older ones are deleted), and `npd list` shows a static app as `published` with
the commit actually being served.

## Commands

```
npd install <name|url>        clone, build, wire up and start
npd update <name> | --all     pull, build, apply changes, restart
npd list [--fetch]            app, port, route, status, commit
npd diff [<name>]             show what would change; writes nothing
npd restart <name>
npd logs <name> [-f]          journalctl for the app
npd secret set <name> <NAME>  prompt for one secret value and store it
                              --stdin reads it from a pipe instead
npd remove <name> [--purge]   remove generated config; --purge also deletes
                              the clone and secrets, after confirmation

npd dev [--prefix] [--build] [--port N]     run this repo locally
```

A bare name resolves to `git@github.com:<NPD_GITHUB_USER>/<name>.git`. Full
git URLs work too.

### `npd dev`

Runs an app from a checkout using the same `npd.toml` the server uses. No
systemd, no nginx, no sudo.

`--prefix` is the useful part. Serving an app bare on `localhost:8000` is
exactly the environment where prefix bugs hide: it works locally and breaks on
deploy. With `--prefix`, `npd` puts a small reverse proxy in front that
reproduces what the generated nginx snippet does — the same redirect, the same
prefix stripping, the same forwarded headers — so those bugs surface on your
laptop instead. The proxy and the nginx renderer are driven by the same
config, so they cannot disagree.

## Deploy on push

A push to an app's default branch can run `npd update <app>` on the server
from GitHub Actions, without giving GitHub a shell:

- One SSH key on the server is **locked to `npd-deploy`** (installed alongside
  `npd`). Whatever a client asks for, that key runs `npd-deploy`, which accepts
  a single app name and runs `npd update` on it. It cannot open a shell, run
  anything else, or pass options.
- **Nothing is sent from GitHub** but that name. `npd update` pulls from the
  app's own remote, so a leaked key can only redeploy what is already pushed.
- **The server's host key is pinned** in the workflow instead of trusting
  whatever answers.
- A failed deploy (build, health check, nginx rollback) exits non-zero, so it
  shows up as a failed run with `npd`'s output in the log. Deploys queue, and
  never run at the same time as each other or as an `npd` command you are
  running by hand.

### Once per server

On your laptop, make the key and read the server's host key:

```console
$ ssh-keygen -t ed25519 -N "" -C npd-deploy -f ~/.ssh/npd_deploy
$ ssh <server> cat /etc/ssh/ssh_host_ed25519_key.pub
```

On the server, add one line to `~/.ssh/authorized_keys` — the public key from
`~/.ssh/npd_deploy.pub`, prefixed with the lock:

```
command="/home/<user>/.local/bin/npd-deploy",restrict ssh-ed25519 AAAA… npd-deploy
```

### Once per app

Add `.github/workflows/deploy.yml` to the app's repository:

```yaml
name: deploy
on: [push, workflow_dispatch]
jobs:
  deploy:
    uses: gnatpat/npd/.github/workflows/deploy.yml@main
    with:
      app: crochet
      server: <user>@example.com
      host_key: example.com ssh-ed25519 AAAA…   # hostname + the host key from above
    secrets: inherit
```

and give it the private key:

```console
$ gh secret set NPD_DEPLOY_KEY -R <you>/<repo> < ~/.ssh/npd_deploy
```

Pushes to other branches show up as skipped runs. `workflow_dispatch` adds a
"Run workflow" button for redeploying by hand. Before pushing a commit that declares a new `[secrets]` entry, run
`npd secret set <app> <NAME>` on the server — a deploy has no terminal to
prompt at, so otherwise that first push fails and asks you to run
`npd update <app>` by hand once.

## When things go wrong

`npd` is built around the idea that a deploy either happens or does not:

- **Bad config** fails before anything is touched.
- **`nginx -t` runs before every reload.** If it fails, every file written in
  that run is restored and nothing is reloaded. A broken render cannot take
  the site down.
- **A failed build** aborts before anything is written, so the old process
  keeps serving, and `npd` tells you the working tree is now ahead of what is
  running. The next `npd update` notices and redeploys — it will not tell you
  everything is fine when it is not.
- **Two commands never interleave.** `install`, `update` and `remove` take a
  lock; a second one prints that it is waiting and runs when the first is done.
- **A failed health check** leaves the service running, prints the last lines
  of its journal, and exits non-zero.
- **Files you wrote yourself are never overwritten.** Every generated file
  starts with a `# Managed by npd` header, and anything without it is left
  alone with an error rather than clobbered.
- **`npd` never deletes a clone and never runs `git clean`.** Only
  `npd remove --purge` deletes anything, it names exactly what it will delete,
  and it asks first.

## Configuration

Defaults assume you are running `npd` as the user that owns the apps. Every
one can be overridden:

| Variable | Default |
|---|---|
| `NPD_USER` | the user running `npd` |
| `NPD_UNIT_PATH` | `~/.local/bin:/usr/local/bin:/usr/bin:/bin` |
| `NPD_GITHUB_USER` | — set this to your own account |
| `NPD_SYSTEMCTL` | `/usr/bin/systemctl` |
| `NPD_PORT_RANGE_START` / `_END` | `8200` / `8300` (exclusive) |
| `NPD_ETC_ENV_DIR` | `/etc/npd/env` |
| `NPD_ETC_SYSTEMD_DIR` | `/etc/npd/systemd` |
| `NPD_NGINX_SNIPPET_DIR` | `/etc/nginx/npd.d` |
| `NPD_STATIC_DIR` | `/var/www/npd` |

`--root <dir>` relocates the entire layout under one directory, which is how
the tests run and a convenient way to see what `npd` would do without letting
it near anything real.

## What it does not do

No zero-downtime deploys — restarts are a `systemctl restart`. No rollback;
recovering is `git checkout` and `npd update`. No management of databases or
one-time setup. No multi-server, no containers, no scheduling. Apps are served
under path prefixes on one domain; subdomains would need a per-domain include
directory and a `domain` key.

## Development

```console
$ uv sync
$ uv run pytest
```

The two pure layers — config and render — are tested by asserting the exact
text of generated files, so a change to a systemd unit or an nginx block shows
up as a failing string comparison rather than a surprise on a server. The
reconcile layer runs against a temp directory with subprocesses recorded
instead of executed. None of it needs root, a server, or systemd.
