"""In-place upgrade. Ported from Bluz's scripts/tests/test_update_offline_package.py
and test_update_overlay_autodetect.py, which covered update.sh."""

from __future__ import annotations

import tarfile

from conftest import make_bundle, run_cli


def _update(deployment, *args):
    return run_cli("update", "--root", str(deployment), "--yes", *args)


def test_offline_package_loads_every_image_refreshes_bundle_and_rolls(
    deployment, tmp_path, calls, monkeypatch
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0

    joined = calls.joined()
    assert "load -i " + str(package / "images" / "demo-ui.tar") in joined
    assert "load -i " + str(package / "images" / "nginx.tar") in joined
    # bundle files replaced, previous copies kept
    assert (deployment / "install.sh").read_text() == "# install v2.0.0\n"
    assert (
        deployment / ".bundle-bak-v1.0.0" / "install.sh"
    ).read_text() == "# install v1.0.0\n"
    # host state untouched, version persisted, env backed up
    assert (deployment / "nginx" / "ssl" / "cert.pem").read_text() == "old-cert\n"
    env = (deployment / ".env").read_text()
    assert "SECRET=keep-me" in env and "DEMO_VERSION=v2.0.0" in env
    assert (deployment / ".env.bak-v1.0.0").exists()
    # images/ never copied into the live deployment
    assert not (deployment / "images").exists()
    rolls = [c for c in joined if " up -d --wait --no-deps " in c]
    assert [r.split()[-1] for r in rolls] == ["ui", "proxy"]


def test_accepts_the_tarball_and_descends_into_its_wrapper_dir(
    deployment, tmp_path, calls, monkeypatch
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    make_bundle(tmp_path / "wrap" / "demo", "v2.0.0", with_images=True)
    archive = tmp_path / "demo-offline-v2.0.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "wrap" / "demo", arcname="demo")

    assert _update(deployment, "--package", str(archive)) == 0
    assert (deployment / "VERSION").read_text().strip() == "v2.0.0"


def test_rejects_the_online_bundle(deployment, tmp_path, calls, capsys):
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=False)
    assert _update(deployment, "--package", str(package)) == 1
    assert "ONLINE bundle" in capsys.readouterr().out
    assert (deployment / "install.sh").read_text() == "# install v1.0.0\n"


def test_rejects_a_package_holding_the_running_version(
    deployment, tmp_path, calls, capsys
):
    package = make_bundle(tmp_path / "pkg", "v1.0.0", with_images=True)
    assert _update(deployment, "--package", str(package)) == 1
    assert "Already running v1.0.0" in capsys.readouterr().out


def test_package_and_version_are_mutually_exclusive(
    deployment, tmp_path, calls, capsys
):
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)
    assert _update(deployment, "--package", str(package), "--version", "v2.0.0") == 1
    assert "cannot be combined" in capsys.readouterr().out
    assert calls.all() == []


def test_missing_package_path_fails_before_touching_anything(
    deployment, tmp_path, calls
):
    assert _update(deployment, "--package", str(tmp_path / "nope.tar.gz")) == 1
    assert not any(" load " in c for c in calls.joined())
    assert not (deployment / ".bundle-bak-v1.0.0").exists()


def test_failed_roll_reports_rollback_and_keeps_backups(
    deployment, tmp_path, calls, monkeypatch, capsys
):
    monkeypatch.setenv("STUB_UP_FAILS", "1")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)
    assert _update(deployment, "--package", str(package)) == 1
    out = capsys.readouterr().out
    assert "Upgrade failed during: rolling ui" in out
    assert "DEMO_VERSION=v1.0.0" in out
    assert ".bundle-bak-v1.0.0" in out


def test_wrong_running_tag_is_caught(deployment, tmp_path, calls, monkeypatch, capsys):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v1.0.0")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)
    assert _update(deployment, "--package", str(package)) == 1
    assert "post-roll verification" in capsys.readouterr().out


def test_refuses_a_stopped_stack(deployment, tmp_path, calls, monkeypatch, capsys):
    monkeypatch.setenv("STUB_RUNNING", "0")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)
    assert _update(deployment, "--package", str(package)) == 1
    assert "No Demo containers are running" in capsys.readouterr().out


# -- overlay autodetect (Bluz#547) ----------------------------------------------


def _compose_calls(calls):
    return [c for c in calls.all() if c[:1] == ["compose"] and c[1:2] != ["version"]]


def test_autodetects_overlay_when_link_hive_persisted_network(
    deployment, tmp_path, calls, monkeypatch
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    (deployment / "docker-compose.hive-local.yml").write_text("services: {}\n")
    with open(deployment / ".env", "a") as env:
        env.write("HIVE_NETWORK_NAME=hive-stack_hive-net\n")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0
    for call in _compose_calls(calls):
        assert str(deployment / "docker-compose.hive-local.yml") in call


def test_no_overlay_when_link_hive_never_ran(deployment, tmp_path, calls, monkeypatch):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    (deployment / "docker-compose.hive-local.yml").write_text("services: {}\n")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0
    for call in _compose_calls(calls):
        assert not any("hive-local" in part for part in call)


def test_explicit_overlay_env_var_still_wins(deployment, tmp_path, calls, monkeypatch):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    custom = tmp_path / "custom-overlay.yml"
    custom.write_text("services: {}\n")
    monkeypatch.setenv("DEMO_COMPOSE_OVERLAY", str(custom))
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0
    assert all(str(custom) in call for call in _compose_calls(calls))


def _edited_deployment(deployment):
    """A deployment installed from a manifest-carrying bundle, then hand-edited."""
    from sb90_deploy.spec import write_manifest

    write_manifest(str(deployment))
    (deployment / "docker-compose.yml").write_text("# hand-edited\n")
    return deployment


def test_hand_edited_files_are_listed_and_replaced_with_yes(
    deployment, tmp_path, calls, monkeypatch, capsys
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    _edited_deployment(deployment)
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0

    out = capsys.readouterr().out
    assert "edited on this host" in out
    assert "        docker-compose.yml" in out
    # install.sh changed between releases but was never hand-edited
    assert "        install.sh" not in out
    assert (
        deployment / ".bundle-bak-v1.0.0" / "docker-compose.yml"
    ).read_text() == "# hand-edited\n"


def test_declining_to_replace_hand_edits_stops_before_rolling(
    deployment, tmp_path, calls, monkeypatch, capsys
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    _edited_deployment(deployment)
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)
    replies = iter(["y", "n"])  # proceed with the upgrade, keep the edits
    monkeypatch.setattr("builtins.input", lambda _prompt: next(replies))

    assert run_cli("update", "--root", str(deployment), "--package", str(package)) == 1

    assert "stopped before replacing" in capsys.readouterr().out
    assert (deployment / "docker-compose.yml").read_text() == "# hand-edited\n"
    assert "DEMO_VERSION=v1.0.0" in (deployment / ".env").read_text()
    assert not [c for c in calls.joined() if " --no-deps " in c]


def test_deployment_without_manifest_warns_and_proceeds(
    deployment, tmp_path, calls, monkeypatch, capsys
):
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")
    package = make_bundle(tmp_path / "pkg", "v2.0.0", with_images=True)

    assert _update(deployment, "--package", str(package)) == 0
    assert "predates the bundle manifest" in capsys.readouterr().out


def test_github_token_prefers_env_then_gh(monkeypatch):
    from sb90_deploy import upgrade

    upgrade.token_and_source.cache_clear()
    monkeypatch.setenv("GH_TOKEN", "env-token")
    assert upgrade.github_token() == "env-token"

    upgrade.token_and_source.cache_clear()
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(upgrade.shutil, "which", lambda _: None)
    assert upgrade.github_token() == ""
    upgrade.token_and_source.cache_clear()


def test_release_lookup_sends_token(monkeypatch):
    import io

    from sb90_deploy import upgrade

    upgrade.token_and_source.cache_clear()
    monkeypatch.setenv("GH_TOKEN", "secret")
    seen = {}

    def fake_urlopen(request, timeout):
        seen.update(request.header_items())
        return io.BytesIO(b'{"tag_name": "v9.9.9"}')

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", fake_urlopen)
    assert upgrade.Upgrade._github(None, "https://api.github.com/x") == {
        "tag_name": "v9.9.9"
    }
    assert seen["Authorization"] == "Bearer secret"
    upgrade.token_and_source.cache_clear()


def _http_error(code, headers=None):
    import email.message
    import urllib.error

    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "Nope", message, None
    )


def _failure(monkeypatch, error, token="", pre_release=False):
    from types import SimpleNamespace

    from sb90_deploy import upgrade

    monkeypatch.setattr(
        upgrade, "token_and_source", lambda: (token, "GH_TOKEN" if token else "")
    )
    me = SimpleNamespace(args=SimpleNamespace(pre_release=pre_release))
    return upgrade.Upgrade._lookup_failure(me, error, "System-B90/Demo")


def test_lookup_failure_names_each_cause(monkeypatch):
    import urllib.error

    reason, hints = _failure(monkeypatch, _http_error(404))
    assert "private" in reason and "no token" in reason
    assert any("GH_TOKEN" in h for h in hints)

    reason, hints = _failure(monkeypatch, _http_error(404), token="t")
    assert "no stable release" in reason and "--pre-release" in hints[0]

    reason, _ = _failure(monkeypatch, _http_error(401), token="t")
    assert "rejected the token from GH_TOKEN" in reason

    reason, _ = _failure(monkeypatch, _http_error(403, {"X-RateLimit-Remaining": "0"}))
    assert "rate limit" in reason

    reason, hints = _failure(monkeypatch, urllib.error.URLError("no route"))
    assert "could not reach api.github.com (no route)" in reason
