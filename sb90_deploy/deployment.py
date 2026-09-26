"""One deployment directory: an extracted bundle (or, for link-hive, a repo checkout)."""

from __future__ import annotations

import os

from . import docker, envfile
from .console import Failure
from .spec import COMPOSE_FILE, OVERLAY_FILE, SPEC_FILE, VERSION_FILE, AppSpec


class Deployment:
    """Paths and derived state for the directory holding app.json + .env.

    `compose_dir` differs from `root` only in a source checkout, where the
    compose files live in deploy/ but .env sits at the repo root.
    """

    def __init__(self, root: str, spec: AppSpec, compose_dir: str | None = None):
        self.root = os.path.abspath(root)
        self.spec = spec
        self.compose_dir = os.path.abspath(compose_dir or self.root)
        override = spec.setting("ENV_FILE")
        self.env_file = os.path.abspath(override or os.path.join(self.root, ".env"))
        compose_override = spec.setting("COMPOSE_FILE")
        self.compose_file = os.path.abspath(
            compose_override or os.path.join(self.compose_dir, COMPOSE_FILE)
        )

    @classmethod
    def locate(cls, root: str | None, app: str | None) -> Deployment:
        """Bundle: app.json at root. Checkout: deploy/app.json, .env one level up."""
        root = os.path.abspath(root or os.getcwd())
        if app:
            spec_path = os.path.abspath(app)
        elif os.path.isfile(os.path.join(root, SPEC_FILE)):
            spec_path = os.path.join(root, SPEC_FILE)
        elif os.path.isfile(os.path.join(root, "deploy", SPEC_FILE)):
            spec_path = os.path.join(root, "deploy", SPEC_FILE)
        else:
            raise Failure(
                f"No {SPEC_FILE} found in {root}.",
                "Run this from the directory you extracted the release into.",
            )
        spec = AppSpec.load(spec_path)
        compose_dir = os.path.dirname(spec_path)
        if os.path.basename(compose_dir) == "deploy" and not os.path.isfile(
            os.path.join(compose_dir, ".env")
        ):
            root = os.path.dirname(compose_dir)
        return cls(root, spec, compose_dir)

    @property
    def is_checkout(self) -> bool:
        return self.compose_dir != self.root

    def env(self) -> dict:
        return envfile.read(self.env_file)

    def overlay_path(self) -> str:
        return os.path.join(self.compose_dir, OVERLAY_FILE)

    def has_overlay(self) -> bool:
        return os.path.isfile(self.overlay_path())

    def uses_overlay(self) -> bool:
        """link-hive persists HIVE_NETWORK_NAME; its presence means the running
        stack is co-located with Hive and must keep the overlay (Bluz#547)."""
        override = self.spec.setting("COMPOSE_OVERLAY")
        if override:
            return True
        return "HIVE_NETWORK_NAME" in self.env() and self.has_overlay()

    def compose_files(self) -> list[str]:
        files = [self.compose_file]
        override = self.spec.setting("COMPOSE_OVERLAY")
        if override:
            files.append(os.path.abspath(override))
        elif self.uses_overlay():
            files.append(self.overlay_path())
        return files

    def compose(self, extra_env: dict | None = None) -> docker.Compose:
        return docker.Compose(self.compose_files(), self.env_file, extra_env)

    def require_compose_file(self) -> None:
        if not os.path.isfile(self.compose_file):
            raise Failure(
                f"{COMPOSE_FILE} not found in {self.compose_dir}.",
                "Run this from the directory you extracted the release into.",
            )

    def bundle_version(self) -> str:
        path = os.path.join(self.root, VERSION_FILE)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        return ""
