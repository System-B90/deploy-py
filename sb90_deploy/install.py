"""First install from an extracted release bundle (was install.sh / install.ps1).

Everything checkable is checked BEFORE compose is invoked: each of these used to
surface as an opaque Docker error several minutes into an install, with nothing
in the message pointing at the actual cause (Bluz#412).
"""

from __future__ import annotations

import errno
import glob
import os
import re
import socket
import subprocess
import sys

from . import docker, envfile
from .console import Failure, banner, info, ok, paint, say, wait, warn
from .deployment import Deployment

_TAG_IN_LOAD = re.compile(r":(v\d+\.\d+\.\d+\S*)$")


def run_setup(deployment: Deployment) -> None:
    """Run the app's own setup.py wizard with this venv's interpreter."""
    setup = os.path.join(deployment.root, "setup.py")
    if not os.path.isfile(setup):
        raise Failure(f"setup.py not found in {deployment.root}.", "The bundle is incomplete.")
    wait("Initializing environment configuration wizard...")
    code = subprocess.call([sys.executable, setup], cwd=deployment.root)
    if code != 0 or not os.path.isfile(deployment.env_file):
        raise Failure(
            "The setup wizard did not produce a .env file.",
            "Re-run it directly to see the failure: ./install.sh (or python3 bootstrap.py setup)",
        )
    ok("Environment configured.")


def _port_in_use(host: str, port: int) -> bool:
    probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((probe, port)) == 0


def _bindable(address: str) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((address, 0))
        except OSError as error:
            return error.errno not in (errno.EADDRNOTAVAIL, 10049)  # 10049: WSAEADDRNOTAVAIL
    return True


def check_ports(deployment: Deployment) -> None:
    spec, env = deployment.spec, deployment.env()
    bind_ip = env.get(spec.bind_ip_var) or "0.0.0.0"
    ports = [env.get(spec.http_port_var) or "80", env.get(spec.https_port_var) or "443"]

    # 127.0.0.2+ are not bindable on Windows until added to the loopback
    # interface, and Docker's error for it is indistinguishable from a port
    # conflict — so say which one it is here.
    if bind_ip not in ("0.0.0.0", "127.0.0.1") and not _bindable(bind_ip):
        hints = [
            f"{spec.bind_ip_var} is set to {bind_ip}, which is not an address on this machine.",
        ]
        if os.name == "nt":
            hints += [
                "Windows does not make 127.0.0.2+ bindable by default. Either:",
                "  1. Add the loopback alias (Administrator, one time):",
                f'       netsh interface ipv4 add address "Loopback Pseudo-Interface 1" {bind_ip} 255.0.0.0',
                f"  2. Or drop {spec.bind_ip_var} from .env and use distinct ports instead",
                f"     ({spec.http_port_var}=8080, {spec.https_port_var}=8443) - no admin needed.",
            ]
        raise Failure("The configured bind address is not available.", *hints)

    # Our own proxy holding the port (a re-run) is not a conflict.
    if docker.container_running(spec.proxy_container):
        return
    for port in ports:
        if port.isdigit() and _port_in_use(bind_ip, int(port)):
            warn(
                f"Something is already listening on port {port}.",
                f"If that is another stack (a local Hive, say), {spec.display_name}'s proxy will",
                "fail to start. Two ways out:",
                f"  1. Give it different ports - set {spec.http_port_var} / {spec.https_port_var} in .env",
                f"  2. Bind both stacks to distinct IPs - set {spec.bind_ip_var} in .env AND make",
                "     the other stack bind a specific IP too (0.0.0.0 reserves the port for",
                "     every address, and on Docker Desktop for Windows even more so).",
            )


def resolve_images(deployment: Deployment) -> tuple[str, bool]:
    """Load offline archives if present; return (version tag, is_offline)."""
    tag = deployment.bundle_version()
    archives = sorted(glob.glob(os.path.join(deployment.root, "images", "*.tar")))
    wait("Resolving Docker images...")
    if not archives:
        info("No local images found. Assuming online mode.")
    else:
        info("Offline bundle detected. Loading local image archives...")
        for archive in archives:
            say(f"   Loading {os.path.basename(archive)}...")
            for reference in docker.load_image(archive):
                match = _TAG_IN_LOAD.search(reference)
                if match and not tag:
                    tag = match.group(1)
        ok(f"Loaded offline images (tag: {tag or 'unknown'}).")
    if not tag:
        # `latest` is never published — only tagged v* builds push images — so
        # falling back to it fails several steps later instead of here.
        raise Failure(
            f"Could not determine which {deployment.spec.display_name} version to run.",
            "The bundle should contain a VERSION file (online) or images/*.tar (offline).",
            "Set it by hand if you know the version: echo v1.0.0 > VERSION",
        )
    return tag, bool(archives)


def install(deployment: Deployment, args) -> int:
    spec = deployment.spec
    banner(f"{spec.display_name} Installer")
    docker.preflight()
    deployment.require_compose_file()

    if os.path.isfile(deployment.env_file):
        ok("Existing .env found. Skipping configuration wizard.")
    else:
        run_setup(deployment)

    for relative in spec.required_files:
        if not os.path.exists(os.path.join(deployment.root, relative)):
            raise Failure(
                f"{relative} is missing.",
                "setup.py writes it - re-run: python3 bootstrap.py setup",
            )

    check_ports(deployment)
    tag, offline = resolve_images(deployment)
    envfile.set_value(deployment.env_file, spec.version_var, tag)

    # Persist HIVE_NETWORK_NAME if the operator set it (co-located installs):
    # compose resolves it on EVERY invocation, so without this the next restart
    # or upgrade falls back to a network that does not exist (Bluz#454).
    if os.environ.get("HIVE_NETWORK_NAME"):
        envfile.set_value(deployment.env_file, "HIVE_NETWORK_NAME", os.environ["HIVE_NETWORK_NAME"])

    compose = deployment.compose()
    wait("Validating compose configuration...")
    if not compose.ok("config", "--quiet"):
        raise Failure(
            "docker-compose.yml did not validate against your .env.",
            "The error above names the missing or malformed variable.",
        )
    if not offline:
        wait(f"Pulling containers ({tag})...")
        if not compose.ok("pull"):
            raise Failure(
                f"Failed to pull images for version {tag}.",
                "If the packages are private, log in first: docker login ghcr.io",
            )
    wait(f"Starting {spec.display_name} services...")
    if not compose.ok("up", "-d", "--wait"):
        raise Failure(
            "Services did not become healthy.",
            f"Inspect with: {compose.describe('logs')}",
        )

    say()
    banner(f"{spec.display_name} Installation Complete!", color="green")
    url = deployment.env().get("NEXTAUTH_URL")
    if url:
        say(f"{spec.display_name} should now be reachable at: {paint('cyan', url)}")
    for line in spec.post_install:
        say(line)
    # SSO registration is the one setup step with a manual fallback; leaving the
    # placeholder in place silently means sign-in is broken post-install.
    if "MANUAL_ENTRY_REQUIRED" in deployment.env().values():
        say(paint("red", "\n[ACTION REQUIRED] Hive SSO is NOT configured - sign-in will fail."))
        say("  .env still contains MANUAL_ENTRY_REQUIRED placeholders.")
        say("  Re-run the wizard to retry registration:  python3 bootstrap.py setup")
    say("\nTo stop the system, run: docker compose down")
    say("To upgrade later, run:   ./update.sh   (Windows: .\\update.ps1)")
    if deployment.has_overlay():
        say("Running Hive on this same machine? Run ./link-hive.sh (.\\link-hive.ps1).")
    return 0
