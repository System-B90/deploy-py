"""Bring the app up against a Hive stack on the SAME Docker daemon (was link-hive.sh).

Only needed for co-located installs; with Hive on another machine ordinary DNS
resolves HIVE_HOSTNAME. What this does that `docker compose up` alone cannot:
give the app's containers a working name for Hive, by aliasing HIVE_HOSTNAME
onto Hive's nginx container inside Hive's network (see each app's
docker-compose.hive-local.yml for why the alias lives there and not on ours).

Works from a bundle root and from a source checkout (deploy/app.json), where it
keeps whatever compose files the running stack was started with (e.g. docker:dev).
"""

from __future__ import annotations

import os
import re

from . import docker, envfile
from .console import Failure, banner, info, ok, say, wait
from .deployment import Deployment

_HIVE_NETWORK = re.compile(r"hive.*net|net.*hive")
_PROXYISH = re.compile(r"nginx|proxy")
CONFIG_FILES_LABEL = "com.docker.compose.project.config_files"


def find_network() -> str:
    wait("Locating Hive's Docker network...")
    result = docker.docker("network", "ls", "--format", "{{.Name}}", capture=True)
    matches = [n for n in result.stdout.split() if _HIVE_NETWORK.search(n)]
    if not matches:
        raise Failure(
            "No Hive network found on this Docker daemon.",
            "Is the Hive stack running? Check with: docker compose ls",
            "If Hive runs on a DIFFERENT machine, you do not need link-hive.",
        )
    if len(matches) > 1:
        raise Failure(
            "Multiple candidate Hive networks found: " + ", ".join(matches),
            "Re-run naming the right one, e.g.:",
            f"  HIVE_NETWORK_NAME={matches[0]} ./link-hive.sh",
        )
    return matches[0]


def find_nginx(network: str, own_prefix: str) -> str:
    result = docker.docker(
        "ps", "--format", "{{.Names}}", "--filter", f"network={network}", capture=True
    )
    names = result.stdout.split()
    for name in names:
        if _PROXYISH.search(name) and own_prefix not in name:
            return name
    raise Failure(
        f"Could not find Hive's nginx container on {network}.",
        "Containers currently on that network: " + (", ".join(names) or "(none)"),
        "Re-run naming it explicitly, e.g.:",
        "  HIVE_NGINX_CONTAINER=hive-nginx ./link-hive.sh",
    )


def ensure_alias(network: str, container: str, hostname: str) -> None:
    wait(f"Aliasing {hostname} onto {container}...")
    template = "{{range .NetworkSettings.Networks}}{{range .Aliases}}{{println .}}{{end}}{{end}}"
    result = docker.docker(
        "inspect", container, "--format", template, check=False, capture=True
    )
    if hostname in (result.stdout or "").split():
        ok("Alias already present - nothing to do.")
        return
    # Already ON the network, so it must be disconnected before it can be
    # reconnected with the extra alias. Reconnect immediately.
    docker.docker("network", "disconnect", network, container, check=False, quiet=True)
    docker.docker("network", "connect", "--alias", hostname, network, container)
    ok("Alias added.")


def compose_files(deployment: Deployment) -> list[str]:
    """The running stack's own compose files plus the overlay; dropping e.g. the
    dev overlay would recreate ui in the wrong mode."""
    running = docker.container_label(deployment.spec.ui_container, CONFIG_FILES_LABEL)
    files = [f for f in running.split(",") if f] or [deployment.compose_file]
    if not any(f.endswith(os.path.basename(deployment.overlay_path())) for f in files):
        files.append(deployment.overlay_path())
    return files


def link_hive(deployment: Deployment, args) -> int:
    spec = deployment.spec
    if not os.path.isfile(deployment.env_file):
        raise Failure(f"No .env found in {deployment.root}.", "Run ./install.sh first.")
    if not deployment.has_overlay():
        raise Failure(
            f"{os.path.basename(deployment.overlay_path())} is missing.",
            "This bundle does not support a co-located Hive.",
        )
    env = deployment.env()
    hostname = os.environ.get("HIVE_HOSTNAME") or env.get("HIVE_HOSTNAME") or "hive.org"

    network = os.environ.get("HIVE_NETWORK_NAME") or find_network()
    ok(f"Using Hive network: {network}")
    # Persist it: compose resolves ${HIVE_NETWORK_NAME} on EVERY invocation, and
    # its presence is what makes update keep the overlay (Bluz#454, #547).
    envfile.set_value(deployment.env_file, "HIVE_NETWORK_NAME", network)

    nginx = os.environ.get("HIVE_NGINX_CONTAINER") or find_nginx(network, spec.name)
    ok(f"Using Hive nginx container: {nginx}")
    ensure_alias(network, nginx, hostname)

    info(f"Bringing {spec.display_name} up with the co-located Hive overlay...")
    compose = docker.Compose(
        compose_files(deployment), deployment.env_file, {"HIVE_NETWORK_NAME": network}
    )
    compose.run("up", "-d")

    banner(f"{spec.display_name} is linked to the local Hive stack.", color="green")
    say("Verify name resolution from inside the ui container:")
    say(
        f"    docker exec {spec.ui_container} node -e \"require('dns').lookup('{hostname}',console.log)\""
    )
    return 0
