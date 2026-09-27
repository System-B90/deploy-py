from __future__ import annotations

import sys

from conftest import make_bundle, run_cli


def test_install_runs_wizard_pins_version_and_boots(tmp_path, calls):
    root = make_bundle(tmp_path / "b", "v3.1.0")
    (root / "setup.py").write_text(
        "open('.env', 'w').write('NEXTAUTH_URL=https://demo.test\\n')\n"
    )

    assert run_cli("install", "--root", str(root)) == 0

    env = (root / ".env").read_text()
    assert "DEMO_VERSION=v3.1.0" in env and "NEXTAUTH_URL=https://demo.test" in env
    joined = calls.joined()
    assert any(" config --quiet" in c for c in joined)
    assert any(c.endswith(" pull") for c in joined)
    assert any(c.endswith(" up -d --wait") for c in joined)


def test_install_offline_loads_images_and_skips_pull(tmp_path, calls):
    root = make_bundle(tmp_path / "b", "v3.1.0", with_images=True)
    (root / ".env").write_text("X=1\n")
    assert run_cli("install", "--root", str(root)) == 0
    joined = calls.joined()
    assert any(c.startswith("load -i ") for c in joined)
    assert not any(c.endswith(" pull") for c in joined)


def test_install_fails_without_version(tmp_path, calls, capsys):
    root = make_bundle(tmp_path / "b", "")
    (root / "VERSION").unlink()
    (root / ".env").write_text("X=1\n")
    assert run_cli("install", "--root", str(root)) == 1
    assert "Could not determine which Demo version" in capsys.readouterr().out


def test_install_persists_hive_network_name(tmp_path, calls, monkeypatch):
    root = make_bundle(tmp_path / "b", "v3.1.0")
    (root / ".env").write_text("X=1\n")
    (root / "docker-compose.hive-local.yml").write_text("services: {}\n")
    monkeypatch.setenv("HIVE_NETWORK_NAME", "hive_net")
    assert run_cli("install", "--root", str(root)) == 0
    assert "HIVE_NETWORK_NAME=hive_net" in (root / ".env").read_text()
    up = [c for c in calls.joined() if " up -d" in c][-1]
    assert "hive-local" in up


def test_link_hive_aliases_persists_and_brings_up_overlay(tmp_path, calls, monkeypatch):
    root = make_bundle(tmp_path / "b", "v3.1.0")
    (root / ".env").write_text("HIVE_HOSTNAME=hive.test\n")
    (root / "docker-compose.hive-local.yml").write_text("services: {}\n")
    monkeypatch.setenv("STUB_NETWORKS", "bridge,hive-stack_hive-net")
    monkeypatch.setenv("STUB_CONTAINERS", "demo-proxy,hive-nginx,hive-db")

    assert run_cli("link-hive", "--root", str(root)) == 0

    assert "HIVE_NETWORK_NAME=hive-stack_hive-net" in (root / ".env").read_text()
    joined = calls.joined()
    assert "network connect --alias hive.test hive-stack_hive-net hive-nginx" in joined
    assert any("hive-local" in c and c.endswith(" up -d") for c in joined)


def test_link_hive_refuses_ambiguous_networks(tmp_path, calls, monkeypatch, capsys):
    root = make_bundle(tmp_path / "b", "v3.1.0")
    (root / ".env").write_text("X=1\n")
    (root / "docker-compose.hive-local.yml").write_text("services: {}\n")
    monkeypatch.setenv("STUB_NETWORKS", "hive_net,other-hive-net")
    assert run_cli("link-hive", "--root", str(root)) == 1
    assert "Multiple candidate Hive networks" in capsys.readouterr().out


def test_checkout_mode_reads_env_at_repo_root(tmp_path):
    from sb90_deploy.deployment import Deployment

    repo = tmp_path / "repo"
    make_bundle(repo / "deploy", "v1")
    (repo / ".env").write_text("X=1\n")
    found = Deployment.locate(str(repo), None)
    assert found.root == str(repo) and found.env_file == str(repo / ".env")
    assert found.compose_file == str(repo / "deploy" / "docker-compose.yml")
    assert sys.executable
