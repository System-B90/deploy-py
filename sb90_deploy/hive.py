"""Hive integrations shared by every app's setup wizard, on top of pyhive.

* `register_sso` — register the app as a Hive SSO application, trying the
  browser flow and the username/password flow (they fail for unrelated reasons)
  and offering a retry before falling back to MANUAL_ENTRY_REQUIRED (Bluz#412).
* `api_client` / `check_api_user` — the service account some apps use for
  background jobs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

from . import prompts
from .console import paint, say

MANUAL = "MANUAL_ENTRY_REQUIRED"
INDEX = "https://system-b90.github.io/.github/pypi/"


def _client_class():
    from pyhive import HiveClient

    return HiveClient


def is_ssh_only_session() -> bool:
    """Terminal-only (SSH, no local browser) session."""
    if not (
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_TTY")
        or os.environ.get("SSH_CLIENT")
    ):
        return False
    if sys.platform.startswith("win"):
        return True
    return not os.environ.get("DISPLAY")


def password_client(hive_url: str, reason: str = "", verify: bool = False):
    """Authenticate with a username/password via HiveClient's own /api/core/token/.

    Deliberately not a hand-rolled OAuth password grant: Hive's default SSO
    client only permits authorization-code, so that returned
    unauthorized_client for every credential (Bluz#412).
    """
    if reason:
        say(paint("yellow", "\n" + reason))
    say("Sign in with a Hive account that can register SSO applications.")
    username = prompts.text("Hive username")
    password = prompts.secret("Hive password")
    return _client_class()(username=username, password=password, hive_url=hive_url, verify=verify)


def api_client(hive_url: str, username: str, password: str, verify: bool = False):
    return _client_class()(
        username=username,
        password=password,
        hive_url=hive_url,
        verify=verify,
        skip_version_check=True,
    )


def check_api_user(hive_url: str, username: str, password: str) -> str | None:
    """None if the credentials work, else the reason they did not."""
    try:
        with api_client(hive_url, username, password) as client:
            client.get_hive_version()
        return None
    except Exception as error:  # noqa: BLE001 - any failure is reported, not raised
        return str(error)


def register_sso(
    service_name: str,
    hive_url: str,
    redirect_uri: str,
    confirm: Callable[[str, bool], bool],
) -> tuple[str, str]:
    """(client_id, client_secret), or MANUAL_ENTRY_REQUIRED placeholders."""
    HiveClient = _client_class()
    attempts: list[Callable] = []
    if not is_ssh_only_session():
        attempts.append(lambda: HiveClient.from_sso(hive_url=hive_url, verify=False))
        attempts.append(
            lambda: password_client(
                hive_url, "Browser sign-in did not complete. Falling back to username/password."
            )
        )
    else:
        attempts.append(
            lambda: password_client(
                hive_url, "No local browser reachable (SSH/terminal-only session)."
            )
        )

    last_error: Exception | None = None
    for build in attempts:
        try:
            client = build()
            credentials = client.register_sso_service(
                service_name=service_name, redirect_uris=redirect_uri
            )
            client_id = credentials.get("client_id", "")
            client_secret = credentials.get("client_secret", "")
            if client_id and client_secret:
                say(paint("green", "Hive SSO registration successful."))
                return client_id, client_secret
            last_error = RuntimeError(
                "Hive accepted the registration but returned no client_id/client_secret."
            )
        except Exception as error:  # noqa: BLE001 - every failure mode is retryable here
            last_error = error
            say(paint("yellow", f"  Attempt failed: {error}"))

    say(paint("red", f"\nFailed to register Hive SSO: {last_error}"))
    say(paint("red", "Sign-in will NOT work until HIVE_CLIENT_ID and HIVE_CLIENT_SECRET are set."))
    say(
        "\nRegister by hand instead (this uses the same working endpoint):\n"
        f"    pip install PyHiveLMS --index-url {INDEX}\n"
        f"    pyhive -u <admin-user> -p <password> register {service_name} --hive-url {hive_url}\n"
        "then copy the returned client_id / client_secret into .env."
    )
    if confirm("Try registering again now?", True):
        return register_sso(service_name, hive_url, redirect_uri, confirm)
    return MANUAL, MANUAL
