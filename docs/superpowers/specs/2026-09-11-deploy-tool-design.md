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

## Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Platform | Thin homegrown tool over existing systemd + nginx | Dokku/Coolify want to own ports 80/443 and nginx, which collides with the existing static site and live apps. Migration cost exceeds the benefit for a hobby box. |
| App shapes supported | Python web service, optionally with a frontend build step | The only shapes actually deployed. Static-only sites and background workers are out of scope until they exist. |
| Repo resolution | Short name → `git@github.com:gnatpat/<name>.git`; full URLs also accepted | Server already has SSH access to the account, so private repos work with no extra credential handling. |
| Routing | Path prefix under natpat.net | Matches what is live today. Subdomains remain a possible later change (see Open questions). |
| Ports | Auto-allocated from 8200–8299, pinnable per app | Removes the need to remember what is where. Pinning lets existing apps migrate without moving. |
| Secrets | `/etc/deploy/env/<app>.env`, mode 0600, never in git | Repos are a mix of public and private; secrets must not depend on that. |
| Build/start commands | Explicit in `deploy.toml` | Convention-guessing is worse to debug than one line of config. |
| Internal shape | Render + reconcile | Makes the risky part a pure function, so it is testable without a server and `diff`/`--dry-run` come free. |
| Privileges | Runs as `nathan`; narrow sudoers for `systemctl` and `nginx`; units registered with `systemctl link` | Avoids running the whole tool as root. |
| Migration of existing apps | Manual runbook, no `adopt` command | Four apps, run once each, then the code would be dead weight. |
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

`install`, `update`, `remove` and `diff` are all thin: they change which apps
exist and with what config, then call reconcile. Reconcile is idempotent — running
it twice in a row makes no writes the second time and reloads nothing.

### On-disk layout

```
~/apps/<name>/                      git clone, owned by nathan
/etc/deploy/state.toml              registry: name → repo, port, route, commit
/etc/deploy/env/<name>.env          secrets, mode 0600
/etc/deploy/systemd/<name>.service  generated unit (nathan-owned)
/etc/nginx/deploy.d/<name>.conf     generated location block(s)
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
client_max_body_size = "10m"

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
- `[env]` entries become `Environment=` lines in the generated unit;
  `[secrets]` values go to the env file loaded via `EnvironmentFile`. A name
  appearing in both is a config error caught by preflight.
- `build.steps` run on every install and update, before the restart. Database
  migrations belong here.

## Generated output

nginx snippet, for `path = "/pokemon/"`:

```nginx
# Managed by deploy — edits will be overwritten
location = /pokemon { return 301 /pokemon/; }
location /pokemon/ {
    client_max_body_size 10m;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /pokemon/;
    proxy_pass http://127.0.0.1:8151/;
}
```

The redirect block is emitted only when `path` ends in `/`.

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

Generated services bind `127.0.0.1`, not `0.0.0.0`. The pokemon app currently
binds `0.0.0.0` and is therefore reachable directly on `:8151`, bypassing nginx
and TLS; migrating it closes that.

Units are registered once per app with `sudo systemctl link
/etc/deploy/systemd/<name>.service`, which exists for units outside the normal
search path. **This must be verified on the box before anything else is built** —
if `link` plus `enable` does not behave, the fallback is to run the tool under
`sudo` and shell out to `runuser -u nathan` for git and build steps.

## Commands

```
deploy install <name|url>     clone → build → wire up → start
deploy update <name|--all>    pull → build → apply changes → restart
deploy list [--fetch]         app, port, route, status, commit; --fetch adds behind-by
deploy diff [name]            show what would change; writes nothing
deploy restart <name>
deploy logs <name> [-f]       journalctl passthrough
deploy remove <name> [--purge]
```

### install

1. Resolve the name to a git URL.
2. Clone to `~/apps/<name>`. If that directory already exists and is a clone of
   the same repo, use it as-is — this is what makes manual migration work.
3. Parse `deploy.toml`; fail with a clear message if missing or invalid.
4. Preflight: name collision, port collision (pinned or allocated), route
   collision. Abort before any mutation.
5. Allocate a port from 8200–8299 unless pinned.
6. Prompt for declared secrets; write the env file at 0600.
7. Run build steps.
8. Render, diff, apply.
9. `systemctl link`, `enable`, `start`.
10. Health check.
11. Record in `state.toml`.

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
- **One real smoke test on the box:** migrate pokemon and confirm it serves.

## Migration runbook

Per app, one at a time, starting with pokemon. Downtime is the gap between
steps 3 and 5.

1. Add `deploy.toml` to the app's repo with `port` pinned to the port it uses
   today. Commit and push.
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

The static site generator is not migrated. It is served directly by nginx from
`/public_html` and does not fit this model.

## Open questions

- **Path-prefix breakage.** Apps must cope with being served under a prefix, and
  pokemon has had trouble with this. Not blocking — the generated snippet
  reproduces what works today, including `X-Forwarded-Prefix`. If prefix
  handling keeps causing pain, the fix is to move to per-app subdomains, which
  needs a wildcard DNS record and a wildcard certificate, and would change
  `[nginx]` to accept `subdomain` as an alternative to `path`.
- **Secret rotation** has no tooling. Editing `/etc/deploy/env/<app>.env` by hand
  and restarting is the process.
