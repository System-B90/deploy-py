"""
Name: test_release.py
Purpose: Unit tests for sb90_deploy.release (the publish command): version math and manifest rewrites,
    plus one end-to-end dry run against a throwaway git repo.
Created: 2026-09-26
Author: Michael K. Steinberg
"""

import json
import subprocess
from pathlib import Path

import pytest

from sb90_deploy import cli, release
from sb90_deploy.release import PY_VERSION, Manifest, Version


def v(text: str) -> Version:
    parsed = Version.parse(text)
    assert parsed is not None
    return parsed


def test_parse_accepts_both_rc_spellings() -> None:
    assert v("v1.2.3-rc.4") == Version(1, 2, 3, 4)
    assert v("1.2.3-rc4") == Version(1, 2, 3, 4)
    assert Version.parse("v1.2") is None


def test_latest_sorts_rc_below_its_release() -> None:
    assert str(release.latest(["v1.0.0-rc.12", "v1.0.0", "v0.9.9", "junk"])) == "1.0.0"


def test_latest_without_tags_is_zero() -> None:
    assert release.latest([]) == Version(0, 0, 0)


@pytest.mark.parametrize(
    ("current", "bump", "rc", "expected"),
    [
        ("1.4.2", "patch", False, "1.4.3"),
        ("1.4.2", "minor", False, "1.5.0"),
        ("1.4.2", "major", False, "2.0.0"),
        ("1.4.2", "patch", True, "1.4.3-rc.1"),
        ("1.4.3-rc.1", "patch", True, "1.4.3-rc.2"),
        # Finishing an rc releases that version, not the next patch.
        ("1.4.3-rc.2", "patch", False, "1.4.3"),
        ("1.4.3-rc.2", "minor", False, "1.5.0"),
    ],
)
def test_next_version(current: str, bump: str, rc: bool, expected: str) -> None:
    assert str(release.next_version(v(current), bump, rc)) == expected


LOCK = {
    "name": "app",
    "version": "1.0.0",
    "packages": {"": {"version": "1.0.0"}, "node_modules/x": {"version": "9.9.9"}},
}


def test_update_manifests(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{\n    "version": "1.0.0"\n}\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps(LOCK, indent=4), encoding="utf-8")
    (tmp_path / "cli.py").write_text("__version__ = '1.0.0'\n", encoding="utf-8")

    updated = release.update_manifests(
        tmp_path,
        [
            Manifest("package.json"),
            Manifest("package-lock.json", count=2),
            Manifest("cli.py", PY_VERSION),
            Manifest("missing.json"),
        ],
        "1.1.0",
    )

    assert updated == ["package.json", "package-lock.json", "cli.py"]
    # Formatting is kept: only the value changes.
    assert (tmp_path / "package.json").read_text(
        encoding="utf-8"
    ) == '{\n    "version": "1.1.0"\n}\n'
    lock = json.loads((tmp_path / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == lock["packages"][""]["version"] == "1.1.0"
    assert lock["packages"]["node_modules/x"]["version"] == "9.9.9"
    assert (tmp_path / "cli.py").read_text(encoding="utf-8") == '__version__ = "1.1.0"\n'


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


APP = {
    "name": "app",
    "env_prefix": "APP",
    "release_repo": "x/app",
    "release": {"manifests": ["package.json", {"path": "v.py", "format": "python"}]},
}


def _repo(tmp_path: Path, branch: str = "master") -> tuple[Path, Path]:
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(tmp_path, "init", "-q", "-b", branch, str(repo))
    for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
        _git(repo, "config", key, value)
    _git(repo, "remote", "add", "origin", str(origin))
    (repo / "deploy").mkdir()
    (repo / "deploy" / "app.json").write_text(json.dumps(APP), encoding="utf-8")
    (repo / "package.json").write_text('{"version": "0.0.0"}\n', encoding="utf-8")
    (repo / "v.py").write_text('__version__ = "0.0.0"\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "tag", "-a", "v1.4.2", "-m", "v1.4.2")
    _git(repo, "push", "-q", "origin", branch, "v1.4.2")
    return origin, repo


def test_publish_dry_run_commits_bump_and_drops_tag(tmp_path: Path) -> None:
    origin, repo = _repo(tmp_path)

    code = cli.main(["publish", "--repo", str(repo), "--bump", "patch", "--no-rc", "-y", "--dry"])

    assert code == 0
    assert (repo / "package.json").read_text(encoding="utf-8") == '{"version": "1.4.3"}\n'
    assert (repo / "v.py").read_text(encoding="utf-8") == '__version__ = "1.4.3"\n'
    assert _git(repo, "log", "-1", "--format=%s") == "chore: bump version to 1.4.3"
    assert _git(repo, "tag", "-l", "v1.4.3") == ""
    assert _git(origin, "tag", "-l") == "v1.4.2"


def test_publish_refuses_other_branches(tmp_path: Path) -> None:
    _, repo = _repo(tmp_path, branch="feature")
    with pytest.raises(SystemExit, match="Must be on master"):
        cli.main(["publish", "--repo", str(repo), "-y"])
