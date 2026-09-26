from __future__ import annotations

import json
import subprocess
import sys
import tarfile

from conftest import APP, COMPOSE

from sb90_deploy import envfile
from sb90_deploy.spec import compose_images, image_archive_name


def test_envfile_round_trip_quotes_only_when_needed(tmp_path):
    path = str(tmp_path / ".env")
    envfile.write(path, {"A": "plain", "B": "has space", "C": "d$ollar", "D": "it's", "E": "x==/+"})
    text = (tmp_path / ".env").read_text()
    assert "A=plain\n" in text and "B='has space'" in text and "C='d$ollar'" in text
    assert "E=x==/+" in text
    assert envfile.read(path) == {
        "A": "plain",
        "B": "has space",
        "C": "d$ollar",
        "D": "it's",
        "E": "x==/+",
    }


def test_set_value_edits_in_place_and_keeps_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep\nA=1\nB='two'\n")
    envfile.set_value(str(path), "A", "9")
    envfile.set_value(str(path), "NEW", "x")
    assert path.read_text() == "# keep\nA=9\nB='two'\nNEW=x\n"


def test_compose_images_resolves_own_tag_and_defaults():
    text = COMPOSE + "  db:\n    image: postgres:${PG:-15-alpine}\n"
    assert compose_images(text, "DEMO_VERSION", "v9") == [
        ("ghcr.io/system-b90/demo/ui:v9", True),
        ("nginx:1.27-alpine", False),
        ("postgres:15-alpine", False),
    ]


def test_archive_names():
    assert image_archive_name("ghcr.io/system-b90/bluz/ui:v1", True, "bluz") == "bluz-ui.tar"
    assert image_archive_name("mongodb/mongodb-community-server:8.0", False, "bluz") == (
        "mongodb-community-server.tar"
    )


def test_bundle_crafts_both_flavours_with_bootstrap_and_shims(tmp_path):
    from sb90_deploy.bundle import craft

    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "deploy" / "docker-compose.release.yml").write_text(COMPOSE)
    (repo / "deploy" / "docker-compose.hive-local.yml").write_text("services: {}\n")
    (repo / "scripts" / "setup.py").write_text("# wizard\n")
    app = dict(
        APP,
        bundle={
            "dir": "demo",
            "files": {
                "docker-compose.yml": "deploy/docker-compose.release.yml",
                "docker-compose.hive-local.yml": "deploy/docker-compose.hive-local.yml",
                "setup.py": "scripts/setup.py",
            },
        },
    )
    (repo / "deploy" / "app.json").write_text(json.dumps(app))

    online, offline = craft(
        str(repo / "deploy" / "app.json"),
        "v1.2.3",
        str(tmp_path / "out"),
        repo=str(repo),
        images="skip",
        wheels=False,
    )

    assert online.name == "demo-online-v1.2.3.tar.gz"
    assert offline.name == "demo-offline-v1.2.3.tar.gz"
    with tarfile.open(online) as tar:
        names = set(tar.getnames())
    for expected in (
        "app.json",
        "VERSION",
        "bootstrap.py",
        "requirements.txt",
        "setup.py",
        "install.sh",
        "install.ps1",
        "update.sh",
        "update.ps1",
        "link-hive.sh",
        "docker-compose.yml",
        "docker-compose.hive-local.yml",
    ):
        assert "demo/" + expected in names, expected
    with tarfile.open(online) as tar:
        assert tar.getmember("demo/install.sh").mode & 0o111
        assert tar.extractfile("demo/VERSION").read() == b"v1.2.3\n"


def test_bundle_guard_rejects_dev_compose(tmp_path):
    from sb90_deploy.bundle import craft
    from sb90_deploy.console import Failure

    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "dc.yml").write_text("services:\n  ui:\n    env_file: ../.env\n")
    (repo / "setup.py").write_text("")
    app = dict(
        APP, bundle={"files": {"docker-compose.yml": "deploy/dc.yml", "setup.py": "setup.py"}}
    )
    (repo / "deploy" / "app.json").write_text(json.dumps(app))
    try:
        craft(
            str(repo / "deploy" / "app.json"),
            "v1",
            str(tmp_path / "o"),
            repo=str(repo),
            images="skip",
            wheels=False,
        )
    except Failure as failure:
        assert "dev compose file" in failure.message
    else:
        raise AssertionError("expected the guard to fail")


def test_bootstrap_rejects_missing_command():
    from pathlib import Path

    import sb90_deploy

    bootstrap = Path(sb90_deploy.__file__).parent / "templates" / "bootstrap.py"
    result = subprocess.run(
        [sys.executable, str(bootstrap)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1 and "usage" in result.stderr


def test_wizard_keeps_secrets_and_foreign_keys(tmp_path, monkeypatch):
    from sb90_deploy.spec import AppSpec
    from sb90_deploy.wizard import Wizard

    monkeypatch.setenv("SB90_WIZARD_DEFAULTS", "1")
    env = tmp_path / ".env"
    env.write_text(
        "NEXTAUTH_SECRET=old\nNEXTAUTH_URL=https://d.test\nDEMO_VERSION=v1\nHIVE_NETWORK_NAME=n\n"
    )
    w = Wizard(AppSpec(APP), env_path=str(env))
    assert w.domain() == "d.test"
    w.ports()
    w.generated("NEXTAUTH_SECRET")
    w.generated("NEW_SECRET")
    w.write()
    values = envfile.read(str(env))
    assert values["NEXTAUTH_SECRET"] == "old" and len(values["NEW_SECRET"]) == 64
    assert values["DEMO_VERSION"] == "v1" and values["HIVE_NETWORK_NAME"] == "n"
    assert values["DEMO_HTTP_PORT"] == "80" and values["NEXTAUTH_URL"] == "https://d.test"


def test_tls_issues_and_then_reuses(tmp_path):
    from sb90_deploy import tls

    tls.ensure_certificate(tmp_path, "demo.test", alt_names=["127.0.0.4"])
    first = (tmp_path / "cert.pem").read_bytes()
    assert tls.covers(tmp_path / "cert.pem", "demo.test")
    tls.ensure_certificate(tmp_path, "demo.test")
    assert (tmp_path / "cert.pem").read_bytes() == first
    tls.ensure_certificate(tmp_path, "other.test")
    assert tls.covers(tmp_path / "cert.pem", "other.test")


def test_wizard_required_domain_fails_cleanly_without_default(tmp_path, monkeypatch):
    import pytest

    from sb90_deploy.spec import AppSpec
    from sb90_deploy.wizard import Wizard

    monkeypatch.setenv("SB90_WIZARD_DEFAULTS", "1")
    w = Wizard(AppSpec(APP), env_path=str(tmp_path / ".env"))
    with pytest.raises(SystemExit, match="required"):
        w.domain()
