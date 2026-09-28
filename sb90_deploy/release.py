"""Cutting a release: the tag that `bundle` and the app's release workflow build from.

    python -m sb90_deploy publish [--bump patch|minor|major] [--rc/--no-rc] [--yes] [--dry]

Derives the next semver from the latest `v*` tag, writes it into the manifests
app.json `release.manifests` lists, commits, tags and pushes. Was each repo's
scripts/publish.py (lifted from Bluz's); prompts use input(), no extra deps.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .spec import AppSpec

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-rc\.?(\d+))?$")
BUMPS = ("patch", "minor", "major")

JSON_VERSION = r'("version"\s*:\s*)"[^"]*"'
PY_VERSION = r"""(__version__\s*=\s*)["'][^"']*["']"""
PATTERNS = {"json": JSON_VERSION, "python": PY_VERSION}


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    rc: int | None = None

    @classmethod
    def parse(cls, text: str) -> "Version | None":
        match = VERSION_RE.match(text.strip())
        if not match:
            return None
        major, minor, patch, rc = match.groups()
        return cls(int(major), int(minor), int(patch), int(rc) if rc else None)

    def sort_key(self) -> tuple[int, int, int, float]:
        # A release candidate sorts below its release: 1.0.0-rc.3 < 1.0.0.
        return (
            self.major,
            self.minor,
            self.patch,
            self.rc if self.rc is not None else float("inf"),
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return base if self.rc is None else f"{base}-rc.{self.rc}"


@dataclass(frozen=True)
class Manifest:
    """A file holding the version. `count` limits how many matches are rewritten
    (package-lock.json: 2, the root's and packages[""]'s)."""

    path: str
    pattern: str = JSON_VERSION
    count: int = 1

    @classmethod
    def from_spec(cls, entry: "str | dict") -> "Manifest":
        """app.json form: "package.json", or {"path", "format": json|python, "count"}."""
        if isinstance(entry, str):
            return cls(entry)
        return cls(
            entry["path"], PATTERNS[entry.get("format", "json")], entry.get("count", 1)
        )


def latest(tags: Sequence[str]) -> Version:
    versions = [v for v in (Version.parse(t) for t in tags) if v is not None]
    return max(versions, key=Version.sort_key, default=Version(0, 0, 0))


def next_version(current: Version, bump: str, rc: bool) -> Version:
    """patch on an rc finishes that rc's version instead of skipping past it."""
    major, minor, patch, cur_rc = (
        current.major,
        current.minor,
        current.patch,
        current.rc,
    )
    if bump == "major":
        major, minor, patch, cur_rc = major + 1, 0, 0, None
    elif bump == "minor":
        minor, patch, cur_rc = minor + 1, 0, None
    elif cur_rc is None:
        patch += 1
    new_rc = (cur_rc + 1 if cur_rc is not None else 1) if rc else None
    return Version(major, minor, patch, new_rc)


def update_manifests(
    root: Path, manifests: Sequence[Manifest], version: str
) -> list[str]:
    """Rewrites each manifest in place; returns the paths that changed."""
    updated = []
    for manifest in manifests:
        path = root / manifest.path
        if not path.is_file():
            print(f"warning: {manifest.path} not found, skipping")
            continue
        text = path.read_text(encoding="utf-8")
        new_text, replaced = re.subn(
            manifest.pattern, rf'\g<1>"{version}"', text, count=manifest.count
        )
        if not replaced:
            print(f"warning: no version in {manifest.path}, skipping")
            continue
        path.write_text(new_text, encoding="utf-8")
        updated.append(manifest.path)
    return updated


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def _ask(message: str, default: str) -> str:
    return input(f"{message} [{default}]: ").strip().lower() or default


def _repo_url(root: Path) -> str | None:
    remote = _git(root, "remote", "get-url", "origin")
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", remote)
    return f"https://github.com/{match.group(1)}" if match else None


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--app", default="deploy/app.json", help="app.json, relative to --repo"
    )
    parser.add_argument("--repo", default=".", help="repo root (default: cwd)")
    parser.add_argument("--bump", choices=BUMPS, help="skip the bump prompt")
    parser.add_argument(
        "--rc", action=argparse.BooleanOptionalAction, help="release candidate"
    )
    parser.add_argument(
        "--version", dest="version", help="explicit version instead of the derived one"
    )
    parser.add_argument(
        "--force", action="store_true", help="allow --version <= latest tag"
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="skip the confirmation"
    )
    parser.add_argument(
        "--dry", action="store_true", help="commit and tag locally, push nothing"
    )


def publish(args: argparse.Namespace) -> int:
    root = Path(args.repo).resolve()
    spec = AppSpec.load(str(root / args.app))
    config = spec.data.get("release", {})
    manifests = [Manifest.from_spec(entry) for entry in config.get("manifests", [])]
    branches = config.get("branches", ["master"])

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch not in branches:
        sys.exit(f"Must be on {' or '.join(branches)} (currently {branch}).")
    if _git(root, "status", "--porcelain"):
        sys.exit("Working tree is not clean.")

    _git(root, "fetch", "--tags", "origin")
    current = latest(_git(root, "tag", "-l", "v*").splitlines())
    print(f"{spec.display_name}: current version v{current}")

    if args.version:
        new = Version.parse(args.version)
        if new is None:
            sys.exit(f"--version {args.version} is not semver.")
        if new.sort_key() <= current.sort_key() and not args.force:
            sys.exit(
                f"v{new} is not newer than v{current}; pass --force to publish it anyway."
            )
    else:
        bump = args.bump or _ask("Bump (patch/minor/major)", "patch")
        if bump not in BUMPS:
            sys.exit(f"Bump must be one of {', '.join(BUMPS)}.")
        rc = (
            args.rc
            if args.rc is not None
            else _ask("Release candidate? (y/n)", "n") == "y"
        )
        new = next_version(current, bump, rc)

    tag = f"v{new}"
    if not args.yes and _ask(f"Publish {tag}? (y/n)", "y") != "y":
        sys.exit("Aborted.")

    updated = update_manifests(root, manifests, str(new))
    if updated:
        _git(root, "add", *updated)
        _git(root, "commit", "-m", f"chore: bump version to {new}")
    _git(root, "tag", "-a", tag, "-m", f"Release {new}")

    if args.dry:
        _git(root, "tag", "-d", tag)
        print(f"Dry run: bump committed locally, nothing pushed, tag {tag} deleted.")
        return 0
    _git(root, "push", "origin", branch)
    _git(root, "push", "origin", tag)
    print(f"Published {tag}.")
    url = _repo_url(root)
    if url:
        print(f"  {url}/releases/tag/{tag}\n  {url}/actions")
    return 0
