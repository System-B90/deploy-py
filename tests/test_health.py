"""The upgrade's health check runs in any image: Node, Python, or one with only wget/curl (Bluz is Node; the
assignment service is Python)."""

from __future__ import annotations

import pytest

from sb90_deploy.docker import Compose

URL = "http://127.0.0.1:8000/healthz"


def compose(tmp_path) -> Compose:
    return Compose([str(tmp_path / "docker-compose.yml")], str(tmp_path / ".env"))


def exec_commands(calls) -> list[str]:
    return [call[call.index("-T") + 2] for call in calls.all() if "exec" in call]


def test_a_node_image_is_checked_with_node(calls, tmp_path):
    healthy, body = compose(tmp_path).exec_health("ui", URL)

    assert healthy
    assert body == {"status": "healthy", "checks": {"db": "ok"}}
    assert exec_commands(calls) == ["node"]


def test_a_python_image_is_checked_with_python(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_EXEC_MISSING", "node")

    healthy, _ = compose(tmp_path).exec_health("service", URL)

    assert healthy
    assert exec_commands(calls) == ["node", "python3"]


def test_an_image_with_only_curl_is_checked_with_curl(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_EXEC_MISSING", "node,python3,python,wget")

    healthy, _ = compose(tmp_path).exec_health("proxy", URL)

    assert healthy
    assert exec_commands(calls)[-1] == "curl"


def test_an_unhealthy_answer_is_not_retried_with_another_client(
    calls, tmp_path, monkeypatch
):
    monkeypatch.setenv("STUB_EXEC_MISSING", "node")
    monkeypatch.setenv("STUB_EXEC_FAILS", "1")

    healthy, body = compose(tmp_path).exec_health("service", URL)

    assert not healthy
    assert body == "connection refused"
    assert exec_commands(calls) == ["node", "python3"]


def test_an_image_without_any_client_says_so(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("STUB_EXEC_MISSING", "node,python3,python,wget,curl")

    healthy, body = compose(tmp_path).exec_health("service", URL)

    assert not healthy
    assert body == "no HTTP client in service (tried node, python3, python, wget, curl)"


@pytest.mark.parametrize("missing", ["", "node"])
def test_the_url_reaches_every_client_as_one_argument(
    calls, tmp_path, monkeypatch, missing
):
    monkeypatch.setenv("STUB_EXEC_MISSING", missing)

    compose(tmp_path).exec_health("service", URL)

    last = calls.all()[-1]
    assert any(URL in part for part in last[last.index("-T") + 2 :])
