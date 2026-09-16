# Backlog

Rough, unordered-within-sections. Found while migrating natpat.net onto npd
(September 2026).

## Bugs

- **`npd dev --prefix` is ignored for static apps.** It serves the output
  directory at `/` regardless. Either route it through the prefix proxy like
  services, or refuse the flag.
- **Republishing the live commit briefly 404s.** `publish` `rmtree`s
  `<name>-<commit>` before copying, but when that commit is already live the
  symlink points straight at the directory being deleted. Copy to a temp
  directory and swap instead.
- **`npd dev` crashes when `.env` is a directory** (`IsADirectoryError`), which
  it is in any repo with an old virtualenv named `.env` (e.g. shogi). Treat a
  non-file `.env` as absent.
- **`npd diff` on an installed service always shows the `NPD_COMMIT` line as
  a pending removal**, because diff deliberately runs no git. Known and
  documented in `commands.diff`, but noisy; could read the stamped commit
  back from the existing unit instead.

## Improvements

- **Detect a relocated virtualenv.** Moving a clone breaks `.venv` (absolute
  shebangs) with a confusing `Failed to spawn: uvicorn`. Detect a venv whose
  paths don't match the clone and tell the user to `rm -rf .venv` (or do it).
- **Make `sandbox = true` work for uv apps.** The sandbox hides `/home`, and
  uv lives there: the binary (`~/.local/bin/uv`), its managed Pythons
  (`~/.local/share/uv/python`) and its cache (`~/.cache/uv`). Options to weigh:
  bind those read-only into the sandbox (the cache needs write access, or
  `UV_NO_CACHE`/a cache inside the clone); run `.venv/bin/<cmd>` directly
  instead of `uv run` so only the venv and its Python are needed; or install
  uv and Pythons system-wide (`/usr/local`, `UV_PYTHON_INSTALL_DIR`) so there
  is nothing in `/home` to expose. Check what each app writes besides its
  clone (pokemon's `collection.db` is in the clone, so that part is fine).
- **Automate per-app deploy setup** once it has been done by hand a few times:
  something that writes `.github/workflows/deploy.yml` and runs
  `gh secret set NPD_DEPLOY_KEY`, so a new repo is one command.
- **`[dev.env]` overrides.** Blog's dev start is
  `env INSTANCE_PATH=$PWD/instance uv run flask ...` only because `[env]`
  can't differ between production and dev.
- **Quieter npm builds.** Consider recommending `npm ci --no-audit --no-fund`
  in the README's examples.

## Around the box (not npd code)

- **`site`'s GitHub Action holds an unrestricted SSH key** (probably the
  unlabelled one in `authorized_keys`), and its push hook runs `site.py`, so
  that secret is effectively root. Remove the key once `site` deploys through
  npd; until then consider deleting it and deploying `site` by hand. Also check
  whether the `DESKTOP-HJOM6N9` and `bethany@Nathans-MacBook-Pro.local` keys
  are still needed.
- **Blog: consider moving the route to `/blog/`** to match the other apps.
- **Move the main site generator (`gnatpat/site`) onto npd.** Today it deploys
  via GitHub Actions → push to `/site.git` → `post-receive` → `deploy.sh`, and
  rebuilds non-atomically by `rmtree`-ing `/public_html/www`. Needed:
  - npd: done — `nginx.path = "/"` is supported. The migration still has to
    delete the hand-written `location /` (and `@manual`) from
    `sites-available/natpat.net` in the same window, since nginx refuses two
    `location /` blocks. `error_page 404 /404/` can stay.
  - site: get the 68 MB of Unity games (`/resources/unity-games`) and `/static`
    (swfs, favicon) into the repo, or accept them as hand-managed inputs.
  - site: replace `requirements.txt` + `/env` with
    `uv run --with jinja2 --with pyyaml python site.py`.
- **Upgrade the droplet off Ubuntu 20.04** (standard support ended April 2025).
  Shogi runs on `python2.7`, which newer releases drop, so port it to Python 3
  as part of that (it has tests: `shogi_test.py`, `shogi_server_test.py`).
- **Delete the `build-deploy-tool` branch.**
