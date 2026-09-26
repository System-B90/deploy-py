"""Fixtures: a stub `docker` (via SB90_DOCKER) and a deployment directory.

The stub appends every invocation to $DOCKER_CALL_LOG and answers the few
queries the flows make. Behaviour is steered by env vars so each test can
fail the flow at the step it cares about:

    STUB_RUNNING=0         `compose ps --status running` reports nothing
    STUB_UP_FAILS=1        every `compose up` exits 42
    STUB_IMAGE=<ref>       what `compose ps --format {{.Image}}` reports
    STUB_NETWORKS=a,b      `docker network ls` output
    STUB_CONTAINERS=a,b    `docker ps` output
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

STUB = r"""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
joined = " " + " ".join(args) + " "
env = os.environ.get
if args[:1] == ["load"]:
    print("Loaded image: ghcr.io/system-b90/demo/ui:" + env("STUB_LOAD_TAG", "v2.0.0"))
    sys.exit(0)
if args[:2] == ["network", "ls"]:
    print("\n".join(filter(None, env("STUB_NETWORKS", "").split(","))))
    sys.exit(0)
if args[:1] == ["ps"]:
    print("\n".join(filter(None, env("STUB_CONTAINERS", "").split(","))))
    sys.exit(0)
if " ps --status running --quiet " in joined:
    print("" if env("STUB_RUNNING") == "0" else "abc123")
    sys.exit(0)
if " ps --format {{.Image}} " in joined:
    print(env("STUB_IMAGE", ""))
    sys.exit(0)
if " up " in joined and env("STUB_UP_FAILS") == "1":
    sys.exit(42)
if " exec " in joined:
    print(json.dumps({"status": "healthy", "checks": {"db": "ok"}}))
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if env("STUB_IMAGES_MISSING") != "1" else 1)
sys.exit(0)
"""

APP = {
    "name": "demo",
    "display_name": "Demo",
    "env_prefix": "DEMO",
    "release_repo": "System-B90/demo",
    "roll": ["ui", "proxy"],
    "containers": {"ui": "demo-ui", "proxy": "demo-proxy"},
    "state": ["nginx/ssl"],
    "bundle": {"dir": "demo", "files": {}},
}

COMPOSE = """name: demo
services:
  ui:
    image: ghcr.io/system-b90/demo/ui:${DEMO_VERSION:?DEMO_VERSION is not set - run ./install.sh}
  proxy:
    image: nginx:1.27-alpine
"""


class Calls:
    def __init__(self, path: Path):
        self.path = path

    def all(self) -> list[list[str]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.all()]


@pytest.fixture
def calls(tmp_path, monkeypatch) -> Calls:
    stub = tmp_path / "docker_stub.py"
    stub.write_text(STUB)
    log = tmp_path / "docker-calls.log"
    monkeypatch.setenv("SB90_DOCKER", str(stub))
    monkeypatch.setenv("DOCKER_CALL_LOG", str(log))
    monkeypatch.setenv("NO_COLOR", "1")
    for name in ("HIVE_NETWORK_NAME", "DEMO_COMPOSE_OVERLAY", "DEMO_VERSION"):
        monkeypatch.delenv(name, raising=False)
    return Calls(log)


def make_bundle(
    root: Path, version: str, with_images: bool = False, extra: dict | None = None
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.json").write_text(json.dumps(APP))
    (root / "docker-compose.yml").write_text(COMPOSE)
    (root / "VERSION").write_text(version + "\n")
    (root / "setup.py").write_text(f"# setup {version}\n")
    (root / "install.sh").write_text(f"# install {version}\n")
    if with_images:
        (root / "images").mkdir()
        (root / "images" / "demo-ui.tar").write_bytes(b"fake")
        (root / "images" / "nginx.tar").write_bytes(b"fake")
    for relative, text in (extra or {}).items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    return root


@pytest.fixture
def deployment(tmp_path) -> Path:
    """A running v1.0.0 deployment with host state that must survive."""
    root = make_bundle(tmp_path / "live", "v1.0.0")
    (root / ".env").write_text("DEMO_VERSION=v1.0.0\nSECRET=keep-me\n")
    (root / "nginx" / "ssl").mkdir(parents=True)
    (root / "nginx" / "ssl" / "cert.pem").write_text("old-cert\n")
    return root


def run_cli(*argv: str) -> int:
    from sb90_deploy.cli import main

    return main(list(argv))


@pytest.fixture
def python() -> str:
    return sys.executable
