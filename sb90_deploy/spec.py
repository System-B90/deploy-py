"""An app's deployment description: `app.json`.

Each app repo keeps one at `deploy/app.json`; the bundler ships it at the bundle
root. Everything the old per-app bash scripts hard-coded — the env-var prefix,
container names, roll order, health endpoint, backup helpers, which files make a
bundle — lives here, so the flows themselves are identical across apps. See
README.md for every key.
"""

import hashlib
import json
import os
import re

COMPOSE_FILE = "docker-compose.yml"
OVERLAY_FILE = "docker-compose.hive-local.yml"
SETUP_SCRIPT = "setup.py"
SPEC_FILE = "app.json"
VERSION_FILE = "VERSION"
# sha256 of every file as shipped; an upgrade compares the live copies against it
# to tell a release's own changes from an operator's hand edits.
MANIFEST_FILE = ".bundle-manifest.json"
# Never hashed: regenerated, swapped wholesale, or too large to be hand-edited.
_UNHASHED = ("images", "wheels", "__pycache__", ".venv")

_IMAGE_LINE = re.compile(r"^\s*image:\s*(.+?)\s*$")
_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?([-?])([^}]*))?\}")


class AppSpec:
    def __init__(self, data, path=None):
        self.data = data
        self.path = path
        self.name = data["name"]
        self.display_name = data.get("display_name", self.name)
        self.env_prefix = data["env_prefix"]
        self.version_var = data.get("version_var", self.env_prefix + "_VERSION")
        self.release_repo = data["release_repo"]
        self.bundle = data.get("bundle", {})
        self.bundle_dir = self.bundle.get("dir", self.name)
        self.roll = list(data.get("roll", ["ui", "proxy"]))
        health = data.get("health", {})
        self.health_service = health.get("service", "ui")
        self.health_url = health.get("url", "http://127.0.0.1:3000/api/health")
        containers = data.get("containers", {})
        self.ui_container = containers.get("ui", self.name + "-ui")
        self.proxy_container = containers.get("proxy", self.name + "-proxy")
        # {"sh": ..., "ps1": ..., "restore_sh": ..., "restore_ps1": ..., "root": ...}
        self.backup = data.get("backup")
        # Files the wizard must have produced before compose can start.
        self.required_files = list(data.get("required_files", []))
        # Host state an upgrade must never overwrite, on top of .env / .venv.
        self.state = list(data.get("state", ["nginx/ssl"]))
        # Operator tooling installed into the bundle's .venv (e.g. bluz-cli).
        self.venv_packages = list(data.get("venv_packages", []))
        self.pip_index = data.get(
            "pip_index", "https://system-b90.github.io/.github/pypi/"
        )
        # Extra lines printed after a successful install.
        self.post_install = list(data.get("post_install", []))

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle), os.path.abspath(path))

    def env_name(self, suffix):
        return f"{self.env_prefix}_{suffix}"

    def setting(self, suffix, default=None):
        """Operator override from the environment, e.g. BLUZ_HEALTH_RETRIES."""
        value = os.environ.get(self.env_name(suffix))
        return default if value in (None, "") else value

    @property
    def bind_ip_var(self):
        return self.env_name("BIND_IP")

    @property
    def http_port_var(self):
        return self.env_name("HTTP_PORT")

    @property
    def https_port_var(self):
        return self.env_name("HTTPS_PORT")

    def online_archive(self, tag):
        return f"{self.bundle_dir}-online-{tag}.tar.gz"

    def offline_archive(self, tag):
        return f"{self.bundle_dir}-offline-{tag}.tar.gz"


def _substitute(value, version_var, tag):
    def replace(match):
        name, operator, argument = match.group(1), match.group(2), match.group(3)
        if name == version_var:
            return tag
        if operator == "-":
            return argument or ""
        return ""

    return _VARIABLE.sub(replace, value)


def compose_images(compose_text, version_var, tag):
    """Every image a compose file runs, resolved for `tag`.

    Returns (reference, is_own) pairs, own meaning tagged by the app's version
    variable. Read from the release compose file itself so the offline archive
    and the online pull can never disagree about which images a release is.
    """
    images = []
    seen = set()
    for line in compose_text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _IMAGE_LINE.match(line)
        if not match:
            continue
        raw = match.group(1).strip("'\"")
        own = ("${" + version_var) in raw
        reference = _substitute(raw, version_var, tag)
        if reference and reference not in seen:
            seen.add(reference)
            images.append((reference, own))
    return images


def image_archive_name(reference, own, app_name):
    """`ghcr.io/system-b90/bluz/ui:v1` -> `bluz-ui.tar`; `postgres:15` -> `postgres.tar`."""
    last = reference.split("/")[-1]
    leaf = last.split(":", 1)[0].split("@", 1)[0]
    return f"{app_name}-{leaf}.tar" if own else leaf + ".tar"


def file_hashes(root):
    """{"relative/posix/path": sha256} for every bundle file under root."""
    hashes = {}
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in _UNHASHED]
        for name in files:
            path = os.path.join(directory, name)
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative == MANIFEST_FILE:
                continue
            with open(path, "rb") as handle:
                hashes[relative] = hashlib.sha256(handle.read()).hexdigest()
    return hashes


def write_manifest(root):
    path = os.path.join(root, MANIFEST_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        json.dump(file_hashes(root), out, indent=2, sort_keys=True)
        out.write("\n")


def read_manifest(root):
    """The manifest the deployment was installed from, or None for older bundles."""
    try:
        with open(os.path.join(root, MANIFEST_FILE), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
