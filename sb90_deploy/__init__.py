"""sb90-deploy — shared release bundling, install and upgrade flows for System-B90 apps.

Two layers:

* `templates/bootstrap.py` + the install/update/link-hive shims ship at every
  bundle root. They only assume the host's Python 3.6+, stay stdlib-only, and
  build `.venv` (Python 3.11+) with this package installed in it.
* Everything else in this package runs inside that venv, so it may use modern
  Python and real dependencies (pyhive, cryptography).

The flows are ports of Bluz's battle-tested bash installers (install.sh,
install.ps1, update.sh, link-hive.sh); the per-app differences live in each
app's `deploy/app.json` (see spec.py).
"""

__version__ = "0.3.0"
