# sb90-deploy

Shared release bundling, install, in-place upgrade, setup-wizard and Hive
integration flows for System-B90's app deployments (Bluz, madash, peek-a-boo).

They were three diverging copies of Bluz's bash/PowerShell scripts. They are now
one Python package, and each app keeps only a `deploy/app.json` describing
itself plus its own `setup.py` questions. The flows are ports of Bluz's
battle-tested scripts; issue numbers in comments (`Bluz#412`, …) point at
the incidents that shaped them.

## What an operator sees

A release bundle (`<app>-online-<tag>.tar.gz` / `<app>-offline-<tag>.tar.gz`)
extracts into `<app>/`:

```
./install.sh              # .\install.ps1 on Windows
./update.sh [--version vX | --package <offline bundle>] [--pre-release] [--skip-backup] [-y]
./link-hive.sh            # only for a Hive on the same Docker daemon
python3 bootstrap.py setup   # re-run the .env wizard
```

The target that matters most is **an air-gapped Ubuntu 22.04 server** with
Docker Compose and apt-installable Python:

1. The shims need any Python ≥ 3.6 and run `bootstrap.py`, which is stdlib-only
   and 3.6-compatible.
2. `bootstrap.py` finds a Python ≥ 3.10 (the stock `python3` on 22.04) and builds
   `.venv` from the bundle's `wheels/` with `--no-index`. If Ubuntu's stripped
   `ensurepip` stops `python3 -m venv` (no `python3-venv`), it falls back to
   `--without-pip` plus the vendored pip wheel.
3. Everything else runs inside `.venv`, as `python -m sb90_deploy <command>`.

The venv is rebuilt when it is missing, too old, or out of date with the
bundle's wheels, so an upgrade that ships new wheels also refreshes the tools.

## Flows

| Command | Module | Was |
| --- | --- | --- |
| `install` | `install.py` | `install.sh`, `install.ps1` |
| `update` | `upgrade.py` | `update.sh` (Bluz; peek-a-boo's copy) |
| `link-hive` | `linkhive.py` | `link-hive.sh`, `link-hive.ps1` |
| `setup` | the app's `setup.py` + `wizard.py`, `tls.py`, `hive.py` | each app's `setup.py` |
| `bundle` | `bundle.py` | each workflow's `craft-release` shell steps |

## `app.json`

```jsonc
{
  "name": "bluz",                      // container/image-archive prefix
  "display_name": "Bluz",
  "env_prefix": "BLUZ",                // BLUZ_VERSION, BLUZ_BIND_IP, BLUZ_HTTP(S)_PORT,
                                       // and operator overrides BLUZ_COMPOSE_FILE,
                                       // BLUZ_COMPOSE_OVERLAY, BLUZ_ENV_FILE,
                                       // BLUZ_RELEASE_REPO, BLUZ_HEALTH_RETRIES,
                                       // BLUZ_BACKUP_SCRIPT, BLUZ_BACKUP_DIR
  "release_repo": "System-B90/Bluz",
  "roll": ["ui", "sessions", "proxy"], // upgrade order: ui first (migrations), proxy last
  "health": {"service": "ui", "url": "http://127.0.0.1:3000/api/health"},
  "containers": {"ui": "bluz-ui", "proxy": "bluz-proxy"},
  "backup": {"sh": "backup/bluz-backup.sh", "ps1": "backup/bluz-backup.ps1",
             "restore_sh": "backup/bluz-restore.sh", "restore_ps1": "backup/bluz-restore.ps1",
             "root": "/var/backups/bluz"},          // omit for stateless apps
  "required_files": [],                // must exist after setup.py (peek-a-boo's token file)
  "state": ["nginx/ssl"],              // host state an upgrade never overwrites
  "venv_packages": ["bluz-cli"],       // operator tooling installed into .venv
  "post_install": ["..."],             // extra lines printed after install
  "bundle": {
    "dir": "bluz",
    "files": {"docker-compose.yml": "deploy/docker-compose.release.yml",
              "docker-compose.hive-local.yml": "deploy/docker-compose.hive-local.yml",
              "setup.py": "scripts/setup.py"},
    "local_wheels": ["cli/dist/*.whl"]
  }
}
```

The bundle's images are **read from the release compose file**. Images tagged
with `${<PREFIX>_VERSION}` are the app's own; the rest (Postgres, Mongo,
nginx) are saved too, since an air-gapped host has no other way to get them.

## A setup.py

```python
from sb90_deploy.wizard import Wizard

w = Wizard()  # reads app.json + existing .env
domain = w.domain(example="bluz.example.com")
w.ports()
w.tls(domain, ssl_dir="nginx/ssl")
hive_url = w.ask("NEXT_PUBLIC_HIVE_URL", "Hive URL", "https://hive.org")
w.generated("NEXTAUTH_SECRET")
w.sso(hive_url)  # browser → password → retry → MANUAL_ENTRY_REQUIRED
w.write()  # keeps keys it does not own
```

## CI

In an app's release workflow, use the org composite action:

```yaml
- uses: System-B90/.github/actions/craft-release@main
  with:
      tag: ${{ github.ref_name }}
```

## Development

```
python -m venv .venv && .venv/bin/pip install -e ".[dev,lint]" --extra-index-url https://system-b90.github.io/.github/pypi/
pytest -q && ruff check . && ruff format --check .
vermin -t=3.6- --no-tips --violations sb90_deploy/templates/bootstrap.py
```

The tests drive the real flows against a stub `docker` (`SB90_DOCKER`), so no
daemon is needed.
