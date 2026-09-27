"""Building blocks for each app's setup.py (.env wizard).

The app keeps its own setup.py, because the questions differ (Bluz's databases,
peek-a-boo's VNC and Mattermost, madash's Prometheus). What is shared is
everything around the questions:

* existing values are offered as defaults, and secrets are kept, not rotated;
* keys the wizard does not own (<APP>_VERSION from install, HIVE_NETWORK_NAME
  from link-hive) survive a re-run;
* the domain / bind-IP / port questions (Bluz#412's shared-host logic);
* TLS (tls.py) and Hive SSO registration (hive.py).

Typical use:

    w = Wizard(app_spec_or_name)
    domain = w.domain()
    w.ports()
    w.tls(domain, ssl_dir="nginx/ssl")
    hive_url = w.ask("NEXT_PUBLIC_HIVE_URL", "Hive URL", default="https://hive.org")
    w.generated("NEXTAUTH_SECRET")
    w.sso(hive_url, redirect_uri=w.url + "/api/auth/callback/hive")
    w.write()

`SB90_WIZARD_DEFAULTS=1` answers every question with its default — for CI.
"""

from __future__ import annotations

import base64
import os
import secrets
import string
from collections import OrderedDict
from collections.abc import Callable

from . import envfile, hive, prompts, tls
from .console import paint, say
from .spec import SPEC_FILE, AppSpec


def hex_key(n_bytes: int = 32) -> str:
    return secrets.token_hex(n_bytes)


def b64_key(n_bytes: int = 32) -> str:
    return base64.b64encode(secrets.token_bytes(n_bytes)).decode()


def password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Wizard:
    def __init__(self, app: AppSpec | str | None = None, env_path: str = ".env"):
        if app is None and os.path.isfile(SPEC_FILE):
            app = AppSpec.load(SPEC_FILE)
        self.spec = app if isinstance(app, AppSpec) else None
        self.display_name = self.spec.display_name if self.spec else (app or "the app")
        self.env_path = env_path
        self.existing = envfile.read(env_path)
        self.values: OrderedDict[str, str] = OrderedDict()
        self.defaults_only = os.environ.get("SB90_WIZARD_DEFAULTS") == "1"
        self.url = ""
        say(f"Starting {self.display_name} interactive environment setup...")

    # -- primitives ------------------------------------------------------------

    def _input(self, message: str, default: str = "") -> str:
        if self.defaults_only:
            say(f"{message} [{default}]" if default else message)
            return default
        return prompts.text(message, default)

    def set(self, key: str, value: str) -> str:
        self.values[key] = value
        return value

    def prev(self, key: str, default: str = "") -> str:
        return self.existing.get(key) or default

    def ask(
        self, key: str | None, message: str, default: str = "", required: bool = False
    ) -> str:
        default = self.prev(key, default) if key else default
        answer = self._input(message, default)
        while required and not answer:
            if self.defaults_only:
                raise SystemExit(
                    f"{message}: required, and there is no default to use."
                )
            say(paint("yellow", "  A value is required."))
            answer = self._input(message)
        return self.set(key, answer) if key else answer

    def ask_secret(self, key: str, message: str) -> str:
        """Blank keeps the existing value."""
        if self.defaults_only:
            return self.set(key, self.prev(key))
        keep = " (blank = keep existing)" if self.prev(key) else ""
        answer = prompts.secret(f"{message}{keep}")
        return self.set(key, answer or self.prev(key))

    def confirm(self, message: str, default: bool = False) -> bool:
        if self.defaults_only:
            say(f"{message} [{'Y/n' if default else 'y/N'}]")
            return default
        return prompts.confirm(message, default)

    def keep(self, key: str, default: str = "") -> str:
        return self.set(key, self.prev(key, default))

    def generated(self, key: str, factory: Callable[[], str] = hex_key) -> str:
        """Existing value, or a fresh one: secrets are never rotated by a re-run."""
        return self.set(key, self.prev(key) or factory())

    # -- shared questions -----------------------------------------------------

    def domain(
        self,
        message: str | None = None,
        example: str = "app.example.com",
        key: str | None = None,
        default: str = "",
    ) -> str:
        """Domain for the app; sets NEXTAUTH_URL (port-aware once ports() ran)."""
        previous = self.existing.get("NEXTAUTH_URL", "")
        from_url = (
            previous.replace("https://", "")
            .replace("http://", "")
            .split(":")[0]
            .rstrip("/")
        )
        default = from_url or default
        if key:
            default = self.prev(key, default)
        message = message or f"Domain name for {self.display_name} (e.g. {example})"
        domain = self.ask(key, message, default, required=True)
        self._set_url(domain)
        self._domain = domain
        return domain

    def _set_url(self, domain: str) -> None:
        port = self.values.get(self.spec.https_port_var, "443") if self.spec else "443"
        self.url = f"https://{domain}" + ("" if port in ("", "443") else f":{port}")
        self.set("NEXTAUTH_URL", self.url)

    def ports(self, default_bind: str = "0.0.0.0") -> None:
        """Where the proxy publishes. Defaults bind every address on :80/:443,
        right when the app has its own host. Sharing a machine with another web
        stack is the case that needs a decision (Bluz#412)."""
        if not self.spec:
            raise ValueError("ports() needs the app spec (app.json)")
        bind_var, http_var, https_var = (
            self.spec.bind_ip_var,
            self.spec.http_port_var,
            self.spec.https_port_var,
        )
        bind_ip = self.prev(bind_var, default_bind)
        http_port = self.prev(http_var, "80")
        https_port = self.prev(https_var, "443")
        shares = self.confirm(
            "Is another web server (e.g. a local Hive stack) already using "
            "ports 80/443 on this machine?",
            default=http_port != "80",
        )
        if shares:
            say(
                paint(
                    "yellow",
                    "\nGiving the app its own ports is the portable way to share a host.\n"
                    "Binding a separate loopback IP also works on Linux, but on Windows it\n"
                    "needs an admin-added loopback alias AND the other stack must stop\n"
                    "binding 0.0.0.0 - Docker Desktop reserves published ports by number.",
                )
            )
            http_port = self.ask(
                None,
                f"HTTP port ({http_var})",
                http_port if http_port != "80" else "8080",
            )
            https_port = self.ask(
                None,
                f"HTTPS port ({https_var})",
                https_port if https_port != "443" else "8443",
            )
            bind_ip = self.ask(
                None, f"Bind address ({bind_var}, 0.0.0.0 = all interfaces)", bind_ip
            )
        self.set(bind_var, bind_ip)
        self.set(http_var, http_port)
        self.set(https_var, https_port)
        if getattr(self, "_domain", None):
            self._set_url(self._domain)

    def tls(
        self,
        hostname: str,
        ssl_dir: str = "nginx/ssl",
        cert_name: str = "cert.pem",
        key_name: str = "key.pem",
    ) -> None:
        alt = []
        if self.spec:
            bind_ip = self.values.get(self.spec.bind_ip_var, "0.0.0.0")
            if bind_ip not in ("0.0.0.0", ""):
                alt.append(bind_ip)
        tls.ensure_certificate(ssl_dir, hostname, cert_name, key_name, alt)

    def sso(
        self,
        hive_url: str,
        redirect_uri: str | None = None,
        service_name: str | None = None,
        id_key: str = "HIVE_CLIENT_ID",
        secret_key: str = "HIVE_CLIENT_SECRET",
    ) -> None:
        """Register (or keep) the app's Hive SSO application."""
        service_name = service_name or self.display_name
        redirect_uri = redirect_uri or f"{self.url}/api/auth/callback/hive"
        client_id, client_secret = self.prev(id_key), self.prev(secret_key)
        register = True
        if client_id and client_secret and client_id != hive.MANUAL:
            register = self.confirm(
                "Existing Hive SSO credentials found. Re-register?", False
            )
        if register and self.defaults_only:
            client_id = client_secret = hive.MANUAL
        elif register:
            say(f"Registering {service_name} SSO service with Hive at {hive_url}...")
            client_id, client_secret = hive.register_sso(
                service_name, hive_url, redirect_uri, self.confirm
            )
        self.set(id_key, client_id)
        self.set(secret_key, client_secret)

    # -- output ---------------------------------------------------------------

    def write(self) -> str:
        """Write .env: the wizard's keys, then every existing key it did not own."""
        merged = OrderedDict(self.values)
        for key, value in self.existing.items():
            merged.setdefault(key, value)
        envfile.write(self.env_path, merged)
        path = os.path.abspath(self.env_path)
        say(paint("green", f"Successfully generated {path}"))
        return path
