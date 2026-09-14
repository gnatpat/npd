# Backlog

Rough, unordered-within-sections. Found while migrating natpat.net onto npd
(September 2026).

## Bugs

- **Unknown keys in `npd.toml` are silently ignored.** Writing `name` and
  `type` at the top level instead of under `[app]` parsed as a *service* app
  and failed with the misleading "a service app requires a [service]
  section". Reject unknown top-level tables/keys (and unknown keys inside
  known tables) with a message naming the key.
- **`npd dev --prefix` is ignored for static apps.** It serves the output
  directory at `/` regardless. Either route it through the prefix proxy like
  services, or refuse the flag.
- **Republishing the live commit briefly 404s.** `publish` `rmtree`s
  `<name>-<commit>` before copying, but when that commit is already live the
  symlink points straight at the directory being deleted. Copy to a temp
  directory and swap instead.
- **`npd diff` on an installed service always shows the `NPD_COMMIT` line as
  a pending removal**, because diff deliberately runs no git. Known and
  documented in `commands.diff`, but noisy; could read the stamped commit
  back from the existing unit instead.

## Improvements

- **Detect a relocated virtualenv.** Moving a clone breaks `.venv` (absolute
  shebangs) with a confusing `Failed to spawn: uvicorn`. Detect a venv whose
  paths don't match the clone and tell the user to `rm -rf .venv` (or do it).
- **A command to change a secret.** Rotating one today means hand-editing
  `/etc/npd/env/<app>.env` and restarting; something like
  `npd secret set <app> <NAME>` could prompt, write 0600, and restart.
- **`[dev.env]` overrides.** Blog's dev start is
  `env INSTANCE_PATH=$PWD/instance uv run flask ...` only because `[env]`
  can't differ between production and dev.
- **Quieter npm builds.** Consider recommending `npm ci --no-audit --no-fund`
  in the README's examples.

## Around the box (not npd code)

- **Non-interactive ssh has no `npd` or `npm` on PATH.** `ssh natpat.net npd list`
  → `command not found`: `~/.local/bin` is added in `~/.profile` (login
  shells only) and nvm in `~/.bashrc` after the interactive guard. Workaround:
  `ssh natpat.net 'bash -lic "npd list"'`. Fix by moving the PATH/nvm lines
  above the guard in `~/.bashrc`.
- **Decommission shogi** (`location /shogi/` in the natpat.net site config,
  `~/shogi`) and the **wedding site** (`~/wedding`, `run-wedding.sh`,
  `sites-enabled/bethany-nathan.wedding`), then `certbot delete` the dead
  bethany-nathan.wedding certificate so renewals stop failing.
- **Pokemon: remove the hardcoded `COLLECTION_PASSWORD` fallback** in
  `server/main.py` (and rotate the password — it was exposed).
- **Blog: consider moving the route to `/blog/`** to match the other apps.
- **Move the main site generator (`gnatpat/site`) onto npd.** Today it deploys
  via GitHub Actions → push to `/site.git` → `post-receive` → `deploy.sh`, and
  rebuilds non-atomically by `rmtree`-ing `/public_html/www`. Needed:
  - npd: allow one static app at `nginx.path = "/"` (currently refused), then
    delete the hand-written `location /` (and `@manual`) from
    `sites-available/natpat.net`. `error_page 404 /404/` can stay.
  - site: get the 68 MB of Unity games (`/resources/unity-games`) and `/static`
    (swfs, favicon) into the repo, or accept them as hand-managed inputs.
  - site: replace `requirements.txt` + `/env` with
    `uv run --with jinja2 --with pyyaml python site.py`.
- **Delete the `build-deploy-tool` branch.**
