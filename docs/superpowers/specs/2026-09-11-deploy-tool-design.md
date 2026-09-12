# `deploy` — a thin deployment tool for natpat.net

**Status:** approved design, not yet implemented
**Date:** 2026-09-11

## Problem

Deploying a new small Python project to the natpat.net droplet is currently five
manual steps: ssh in, clone the repo, hand-write `run-<app>.sh` and
`update-<app>.sh`, hand-write a systemd unit, and hand-edit a `location` block
into `/etc/nginx/sites-available/natpat.net`. Ports are picked by memory
(8008, 8080, 8151, 8152). Secrets live in plaintext inside the run scripts.
As the number of apps grows this is increasingly error-prone and tedious.

## Goal

`deploy install <repo>` on the server does the whole setup. `deploy update <app>`
pulls and redeploys. Nothing else changes about how the box works.

## Non-goals

- Containers, a registry, or an orchestrator. The apps are small and the droplet
  is small.
- Replacing nginx, the static site generator, or certbot's management of TLS.
- Managing app data, databases, or one-time setup. Apps that need a database
  get it created by hand; apps that need a data directory declare a path in
  `[env]` and the owner moves data there per-app.
- Rollback. A failed deploy leaves the previous process running; recovering is a
  manual `git checkout` plus `deploy update`.
- Zero-downtime deploys. Restarts are a `systemctl restart`.
- A full local mirror of the server. `deploy dev` runs one app and optionally
  proxies it under its prefix; it does not serve TLS or any of the other apps.

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Platform | Thin homegrown tool over existing systemd + nginx | Dokku/Coolify want to own ports 80/443 and nginx, which collides with the existing static site and live apps. Migration cost exceeds the benefit for a hobby box. |
| App shapes supported | Python web service, and static sites | Both are actually deployed today. Background workers remain out of scope until one exists. |
| Repo resolution | Short name → `git@github.com:gnatpat/<name>.git`; full URLs also accepted | Server already has SSH access to the account, so private repos work with no extra credential handling. |
| Routing | Path prefix under natpat.net | Matches what is live today. Subdomains remain a possible later change (see Open questions). |
| Ports | Auto-allocated from 8200–8299, pinnable per app | Removes the need to remember what is where. Pinning lets existing apps migrate without moving. |
| Persistent state | None. The generated unit is the record | Every other fact (repo, route, commit, which apps exist) is derivable from the filesystem and git, and derived state cannot go stale. A separate registry could disagree with the units, and for a reconcile tool there is no principled answer to which wins. |
| Secrets | `/etc/deploy/env/<app>.env`, mode 0600, never in git | Repos are a mix of public and private; secrets must not depend on that. |
| Build/start commands | Explicit in `deploy.toml` | Convention-guessing is worse to debug than one line of config. |
| Internal shape | Render + reconcile | Makes the risky part a pure function, so it is testable without a server and `diff`/`--dry-run` come free. |
| Privileges | Runs as `nathan`; narrow sudoers for `systemctl` and `nginx`; units registered with `systemctl link` | Avoids running the whole tool as root. |
| Migration of existing apps | Manual runbook, no `adopt` command | Four apps, run once each, then the code would be dead weight. |
| Local dev | `deploy dev`, with an optional prefix-reproducing proxy | The same config should drive both sides, and prefix bugs must be reproducible off the server. |
| Tool's home | Its own repo, `gnatpat/deploy` | Server tooling should not be coupled to local dotfiles. |

## Architecture

Three layers, with the dependencies pointing one way:

1. **Config** — parse `deploy.toml` into a validated `AppConfig`. Pure. No IO.
2. **Render** — `(AppConfig, port, paths) → dict[Path, str]`, the complete set of
   generated files and their exact contents. Pure. No IO. This is where the
   systemd unit and nginx snippet text is produced, and where the tests bite.
3. **Reconcile** — diff the rendered dict against what is on disk, write only
   what differs, and trigger only the reloads that the changes require.
   All IO and all subprocess calls live here, behind injected interfaces.

There is no database and no registry file. The generated unit *is* the record:
it carries the assigned port as `Environment=PORT=`, and everything else the
tool reports is read back from git, the filesystem and `systemctl` on demand.
An app is installed if and only if it owns a generated artifact — a unit, an
nginx snippet, or both. Only services have units, so the port scan reads units
alone; static apps need no port.

`install`, `update`, `remove` and `diff` are all thin: they change which apps
exist and with what config, then call reconcile. Reconcile is idempotent — running
it twice in a row makes no writes the second time and reloads nothing.

### On-disk layout

```
~/apps/<name>/                      git clone, owned by nathan
/etc/deploy/env/<name>.env          secrets, mode 0600
/etc/deploy/systemd/<name>.service  generated unit (nathan-owned)
/etc/nginx/deploy.d/<name>.conf     generated location block(s)
/var/www/deploy/<name>              static apps: symlink → <name>-<commit>
```

### One-time server setup

1. Add `include /etc/nginx/deploy.d/*.conf;` inside the existing `natpat.net`
   server block in `/etc/nginx/sites-available/natpat.net`. The tool never
   touches that file again; the static site, `/static/`, error pages and the
   certbot-managed TLS lines stay exactly as they are.
2. Create `/etc/deploy/` and subdirectories, owned by `nathan`.
3. Add `/etc/sudoers.d/deploy` permitting, without password:
   `systemctl`, `nginx -t`, and reloading nginx.
4. Install the tool: `uv tool install git+ssh://git@github.com/gnatpat/deploy`.

nginx matches prefix `location` blocks by longest match rather than file order,
so the position of the `include` within the server block does not matter.

## Config format

`deploy.toml`, at the repo root:

```toml
[app]
name = "pokemon"            # optional — defaults to the repo name
type = "service"            # "service" (default) or "static"

[build]                     # omit entirely if there is nothing to build
workdir = "pokemon-cards-app"
steps = ["npm install", "npm run build"]

[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
health_path = "/"           # optional; GET after restart, must return 2xx
                            # absent → TCP connect to $PORT is the only check
# port = 8151               # optional pin — set when migrating an existing app

[nginx]                     # omit for an internal-only service
path = "/pokemon/"
strip_prefix = false        # default true; see "Prefix handling" below
client_max_body_size = "10m"

[dev]                       # optional; local overrides for `deploy dev`
start = "uv run uvicorn main:app --reload --port $PORT"

[env]                       # non-secret, committed
LOG_LEVEL = "info"

[secrets]                   # names and descriptions only, never values
COLLECTION_PASSWORD = "password for the collection upload endpoint"
```

Rules:

- `$PORT` is always injected into the environment.
- An absent section means "do not do that": no `[build]` means no build step,
  no `[nginx]` means no public route.
- `[secrets]` maps a variable name to a human description. `install` prompts for
  each value and writes the env file. `update` prompts for any newly declared
  secret not already present, so adding a secret to a repo does not require
  separately remembering to edit a file on the server.
- `[dev]` overrides `[service]` for local runs only. Absent keys fall back to
  `[service]`, so a repo with no `[dev]` section runs locally exactly what runs
  in production.
- `[env]` entries become `Environment=` lines in the generated unit;
  `[secrets]` values go to the env file loaded via `EnvironmentFile`. A name
  appearing in both is a config error caught by preflight.
- `build.steps` run on every install and update, before the restart. Database
  migrations belong here.

## Generated output

nginx snippet, for `path = "/pokemon/"` with `strip_prefix = false`:

```nginx
# Managed by deploy — edits will be overwritten
location = /pokemon { return 301 /pokemon/; }
location /pokemon/ {
    client_max_body_size 10m;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /pokemon/;
    proxy_pass http://127.0.0.1:8151;
}
```

The redirect block is emitted only when `path` ends in `/`.

### Prefix handling

`strip_prefix` controls one character of output — the trailing slash on
`proxy_pass` — and it changes what the app receives:

| `strip_prefix` | generated | request `/pokemon/cards` arrives as |
|---|---|---|
| `true` (default) | `proxy_pass http://127.0.0.1:PORT/;` | `/cards` |
| `false` | `proxy_pass http://127.0.0.1:PORT;` | `/pokemon/cards` |

`X-Forwarded-Prefix` is sent in both cases, so an app that strips can still
build correct absolute URLs.

This is not a cosmetic setting and it is the most likely cause of past trouble
with `/pokemon`. In the current hand-written config, `/blog` and `/crochet`
strip, while `/pokemon` alone does not — pokemon is the only app that receives
its own prefix. **When writing each app's `deploy.toml`, copy
whatever that app does today**; changing it silently breaks every route.

systemd unit:

```ini
# Managed by deploy — edits will be overwritten
[Unit]
Description=pokemon (managed by deploy)
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=nathan
WorkingDirectory=/home/nathan/apps/pokemon/server
Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PORT=8151
EnvironmentFile=/etc/deploy/env/pokemon.env
ExecStart=/bin/bash -c 'exec uv run uvicorn main:app --host 127.0.0.1 --port $PORT'
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
```

`PATH` is set explicitly rather than using a login shell, so units do not
silently depend on `.bashrc` locating `uv`.

Generated services bind `127.0.0.1`, not `0.0.0.0`. All four current apps bind
`0.0.0.0`; ufw blocks those ports from the internet today (verified: 8008, 8080,
8151 and 8152 are all filtered externally), so this is defence in depth rather
than closing an open hole — it removes the dependency on a firewall rule staying
correct.

Units are registered once per app with `sudo systemctl link
/etc/deploy/systemd/<name>.service`, which exists for units outside the normal
search path. **Verified on the box, 2026-09-11** (systemd 245, Ubuntu 20.04) —
see Verified facts below. The `sudo`-plus-`runuser` fallback is not needed.

### Static apps

`type = "static"` has no process, no port, no secrets and no unit. `[build]`
gains an `output` key naming the directory the build produces:

```toml
[app]
name = "boggle"
type = "static"

[build]
steps = ["uv run python make_static.py"]
output = "static"           # relative to the repo root

[nginx]
path = "/boggle/"
```

Publishing copies the built output to `/var/www/deploy/<name>-<commit>` and then
atomically swaps the `/var/www/deploy/<name>` symlink onto it, so a rebuild never
serves a half-written tree. The previous build is kept, which makes rolling a
static site back a symlink swap.

Generated nginx:

```nginx
# Managed by deploy — edits will be overwritten
location = /boggle { return 301 /boggle/; }
location /boggle/ {
    alias /var/www/deploy/boggle/;
    try_files $uri $uri/ =404;
}
```

Preflight rejects `[service]`, `[secrets]` or `strip_prefix` on a static app, and
requires `build.output`. `deploy dev` for a static app builds and serves the
output directory locally, honouring `--prefix` the same way.

## Verified facts

Checked directly on `natpat.net` on 2026-09-11. Ubuntu 20.04, systemd 245,
nginx 1.18.0, `uv` at `/home/nathan/.local/bin/uv`.

- **`systemctl link` works.** Linking a unit from outside the search path,
  `enable` honouring its `[Install]` section, `start`, and — the property
  reconcile depends on — editing the file in place followed by `daemon-reload`
  causes systemd to pick up the new contents. `disable` removes both the unit
  symlink and the `multi-user.target.wants` symlink, leaving no trace.
- **`systemctl` is already passwordless:** `/etc/sudoers.d/site` grants
  `(ALL) NOPASSWD: /usr/bin/systemctl`.
- **`nginx -t` and reload are not.** They prompt for a password, so
  `/etc/sudoers.d/deploy` granting them is genuinely required; the tool cannot
  reconcile nginx until it exists.
- **`journalctl -u <app>` works as `nathan` with no sudo**, because the units run
  as `User=nathan` and a user can read their own services' logs. `nathan` is in
  neither `adm` nor `systemd-journal`. `deploy logs` needs no privileges.
- **Ports in use:** 8080 (blog), 8151 (pokemon), 8152 (crochet), plus 8008 for
  shogi until it is decommissioned — all outside the 8200–8299 allocation
  range, so no migration collides.

## Commands

```
deploy install <name|url>     clone → build → wire up → start
deploy update <name|--all>    pull → build → apply changes → restart
deploy list [--fetch]         app, port, route, status, commit; --fetch adds behind-by
                              (all read live from units, git and systemctl)
deploy diff [name]            show what would change; writes nothing
deploy restart <name>
deploy logs <name> [-f]       journalctl passthrough
deploy remove <name> [--purge]

deploy dev [--prefix] [--build] [--port N]    run locally, from a repo checkout
```

### install

1. Resolve the name to a git URL.
2. Clone to `~/apps/<name>`. If that directory already exists and is a clone of
   the same repo, use it as-is — this is what makes manual migration work.
3. Parse `deploy.toml`; fail with a clear message if missing or invalid.
4. Preflight: name collision, port collision (pinned or allocated), route
   collision. Abort before any mutation.
5. Allocate a port (services only): scan `/etc/deploy/systemd/*.service` for `Environment=PORT=`,
   take the lowest free port in 8200–8299. Skipped if `[service] port` is pinned.
6. Prompt for declared secrets; write the env file at 0600. (Services only.)
7. Run build steps.
8. For a static app, publish `build.output` to `/var/www/deploy/<name>-<commit>`
   and swap the symlink.
9. Render, diff, apply.
10. `systemctl link`, `enable`, `start`. (Services only.)
11. Health check. (Services only; a static app is verified by the nginx reload
    succeeding.)

### update

1. Fast-forward-only `git pull`. Abort on a dirty tree or diverged history.
2. If there are no new commits and the rendered output is unchanged, report
   "already up to date" and stop.
3. Re-parse config; preflight; prompt for newly declared secrets.
4. Run build steps.
5. Render, diff, apply.
6. Restart the service only if the code or the unit changed. Reload nginx only
   if a snippet changed.
7. Health check.

## Local development

`deploy dev` runs an app from a repo checkout on the laptop, using the same
`deploy.toml` the server uses. No systemd, no nginx, no sudo, no state file — it
reuses only the config layer.

1. Read `./deploy.toml` from the current directory.
2. Resolve environment: `[env]` from the config, plus a gitignored `.env` in the
   repo root supplying `[secrets]` values. If a declared secret is missing,
   fail listing the names and their descriptions rather than starting a process
   that will die confusingly later.
3. Run `[build] steps` only when `--build` is passed. Builds are slow and rarely
   needed between runs; the first run of a repo with a frontend needs it.
4. Run `[dev] start` if present, otherwise `[service] start`, in the foreground
   from the appropriate `workdir`. Ctrl-C terminates the child cleanly.

Ports: the app binds `$PORT`, default 8000. Production port assignments are
irrelevant locally.

### `--prefix`

Serving the app bare on `localhost:8000` is precisely the environment where
prefix bugs hide, so `--prefix` reproduces what nginx does in production. The
app is started on an internal ephemeral port and a small reverse proxy listens
on `$PORT` instead, so the browser URL is the same in both modes.

The proxy mirrors the generated snippet exactly, and is the only part of local
dev with real logic:

- `GET /pokemon` → 301 `/pokemon/`, when `path` ends in `/`.
- Requests under `path` are forwarded, stripping the prefix or not according to
  `strip_prefix`.
- Sets `Host`, `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Prefix`
  with the same values nginx sends.
- Anything outside `path` returns 404, as it would on the server.

The proxy and the nginx renderer are driven by the same `AppConfig`, so
`strip_prefix` cannot mean one thing locally and another in production.

This is deliberately not a full local mirror: no TLS, no other apps, and no
nginx behaviour beyond routing and forwarded headers. It exists to catch prefix
and header bugs before deploy.

## Failure behaviour

- Preflight runs before any mutation. Bad config, port clash or route clash
  aborts having touched nothing.
- `nginx -t` runs before every reload. On failure, the previous snippet contents
  are restored, the command aborts, and nginx's own error is printed.
- A failed build aborts before the restart, so the old process keeps serving.
  The tool reports that the working tree is now ahead of what is running.
- A failed health check leaves the service up, prints the last 20 journal lines,
  and exits non-zero.
- Any file missing the `Managed by deploy` header is never overwritten, so a
  hand-written unit cannot be silently clobbered.
- **The tool never deletes a clone and never runs `git clean`.** App data
  currently lives inside clones. Only `remove --purge` deletes anything, and it
  names what it will delete and asks first.

## Testing

- **Render layer:** table tests asserting exact generated text for a range of
  configs — with and without `[build]`, with and without `[nginx]`, trailing and
  non-trailing route paths, with and without secrets. Pure functions: no root,
  no server, fast.
- **Reconcile layer:** a `--root` override points all writes at a temp
  directory. Assert which files change, that a second identical run writes
  nothing and reloads nothing, that a failed `nginx -t` restores prior content,
  and that the managed-header guard refuses to overwrite a foreign file.
- **Command runner** is injected, so `systemctl`, `git` and build steps are
  recorded rather than executed.
- **Static publishing:** assert the symlink swap is atomic from the reader's
  point of view, that the previous build is retained, and that a failed build
  leaves the live symlink pointing at the last good version.
- **Port allocator:** given a set of existing unit files, assert it picks the
  lowest free port, skips pinned ports, respects the range bounds, and errors
  clearly when the range is exhausted.
- **Local proxy:** tested against a stub upstream that echoes the path and
  headers it received. Assert that `strip_prefix = true` and `false` deliver the
  paths in the Prefix handling table, that the redirect fires only for a
  trailing-slash `path`, that forwarded headers match what the nginx renderer
  emits, and that a request outside `path` 404s.
- **One real smoke test on the box:** migrate pokemon and confirm it serves.

## Inventory

Surveyed 2026-09-11, from the running server and the local checkouts.

| App | Repo | Starts via | Port | Route | `strip_prefix` | Data |
|---|---|---|---|---|---|---|
| ~~shogi~~ *(decommission)* | `gnatpat/shogi` | `~/shogi/run.sh` → `python2.7 shogi_server.py` | 8008 | `/shogi/` | — | — |
| blog | `gnatpat/blog` | `uv sync`, venv activate, `INSTANCE_PATH=/blog ./run-blog` | 8080 (implicit) | `/blog` | `true` | `/blog`, outside the clone |
| crochet | `gnatpat/crochet` | `uvicorn server:app` in `server/` | 8152 | `/crochet/` | `true` | `crochet.db` inside the clone |
| pokemon | `gnatpat/pokemon` | `uvicorn main:app` in `server/`, plus a secret | 8151 | `/pokemon/` | `false` | `collection.db` inside the clone |
| boggle | `gnatpat/boggle-solver` | `release.sh` scp's `static/*` to the server | — | `/boggle/` | n/a | — |
| site | `gnatpat/site` | `/site.git` post-receive hook | — | `/` | n/a | `/public_html/www` |

Notes that affect the migration:

- **Repo name, local directory name and route often differ.** `pokemon-cards`
  locally is `gnatpat/pokemon` on GitHub and `~/pokemon` on the server;
  `boggle-ocr` locally is `gnatpat/boggle-solver`. `install` resolves the repo
  name; `[app] name` overrides when the route should differ.
- **blog's port is implicit.** `waitress-serve` with no `--port` defaults to
  8080. Its `start` must become `waitress-serve --port=$PORT --call
  'blog:create_app'`, or the injected port is silently ignored.
- **blog already keeps data outside the clone** via `INSTANCE_PATH`. That is the
  pattern for crochet and pokemon to adopt when convenient; the tool does not
  impose it.
- **shogi is out of scope** — a hand-written socket server on Python 2.7, to be
  decommissioned rather than migrated. With it gone, every remaining app is a
  `uv`-managed Python project, though `start` stays an arbitrary command.
- **`site.service` is vestigial** — disabled, no journal entries, and not how the
  site deploys. It should be deleted. It is also a trap: `Type=simple` with
  `Restart=always` around `site.py`, which generates and exits, so starting it
  would rebuild `/public_html/www` in a loop, `rmtree`-ing the live directory on
  each pass. Out of scope for migration; the bare-repo hook keeps working.

## Server housekeeping

Found while surveying, unrelated to this tool but recorded so it is not lost:

- **`certbot.service` fails daily, but natpat.net is not at risk.** Diagnosed
  2026-09-12: the failure is entirely
  `/etc/letsencrypt/renewal/bethany-nathan.wedding.conf`, whose domain is now
  NXDOMAIN, so the http-01 challenge cannot succeed. natpat.net renews cleanly
  in the same run. The unit's failed state is stale-config noise, not a TLS
  countdown — but it masks any real failure that appears later, which is the
  reason to fix it.
- `fwupd-refresh.service` is also failed; harmless.

### Decommissioning

Neither is part of the tool's scope; both remove work from it.

**`bethany-nathan.wedding`** — the domain is gone; the server still carries its
config:

1. `sudo certbot delete --cert-name bethany-nathan.wedding` (stops the failures)
2. remove the nginx site from `sites-enabled` and `sites-available`, `nginx -t`,
   reload
3. `sudo systemctl stop wedding && sudo systemctl disable wedding`, remove
   `/etc/systemd/system/wedding.service`
4. remove `~/run-wedding.sh`; **back up `/wedding/wedding.sqlite` and
   `/wedding/backups/` before deleting `/wedding`**

**shogi** — to be taken down rather than migrated. A from-scratch socket server
on Python 2.7; keeping it alive is the only thing that would force the tool to
support non-`uv` Python.

1. `sudo systemctl stop shogi && sudo systemctl disable shogi`, remove
   `/etc/systemd/system/shogi.service`
2. remove the `location /shogi/` block from `sites-available/natpat.net`,
   `nginx -t`, reload
3. keep or archive `~/shogi` and the repo; nothing else depends on it

## Migration runbook

Four apps migrate: blog, crochet and pokemon as services, boggle as static.
shogi and wedding are decommissioned instead; site keeps its bare-repo hook.
One at a time, starting with pokemon. Downtime is the gap between
steps 3 and 5.

1. Add `deploy.toml` to the app's repo with `port` pinned to the port it uses
   today, and `strip_prefix` set to match the app's current `proxy_pass` line —
   a trailing slash means `strip_prefix = true`. Getting this wrong breaks every
   route in the app. Today: `/blog` and `/crochet` are `true`,
   `/pokemon` is `false`. Verify with `deploy dev --prefix` before deploying.
   Commit and push.
2. `mv ~/pokemon ~/apps/pokemon` — **move, do not re-clone**, because app data
   currently lives inside the clone.
3. Remove the app's `location` blocks from
   `/etc/nginx/sites-available/natpat.net`.
4. `sudo systemctl stop <app> && sudo systemctl disable <app>` and remove
   `/etc/systemd/system/<app>.service`.
5. `deploy install <app>`.
6. Delete the old `~/run-<app>.sh` and `~/update-<app>.sh`.
7. Move any secret out of the old run script into the prompted env file, and
   rotate it — `COLLECTION_PASSWORD` has been sitting in plaintext.

If step 3 is forgotten, nginx reports a duplicate location and `nginx -t`
refuses the change before anything goes live. The failure mode is safe.

boggle migrates as a `type = "static"` app, replacing `release.sh`'s scp into
`/resources/boggle/` and `/public_html/www/boggle/`. Those two copies should be
removed once the route is served from `/var/www/deploy/boggle`.

The site generator itself is not migrated. It is served directly by nginx from
`/public_html` via the `/site.git` post-receive hook and keeps working as-is.

## Open questions

- **Path-prefix breakage.** Largely addressed: `strip_prefix` makes the
  behaviour explicit per app instead of an accident of a trailing slash, and
  `deploy dev --prefix` reproduces it locally. If it still causes pain, the
  remaining fix is per-app subdomains, which needs a wildcard DNS record and a
  wildcard certificate, and would change `[nginx]` to accept `subdomain` as an
  alternative to `path`.
- **Single domain only.** `include /etc/nginx/deploy.d/*.conf;` wires apps into
  the `natpat.net` server block alone. The box also serves
  `bethany-nathan.wedding`, which is slated for deletion. Supporting a second
  domain would mean a per-domain include directory and a `domain` key in
  `[nginx]`.
- **Secret rotation** has no tooling. Editing `/etc/deploy/env/<app>.env` by hand
  and restarting is the process.
