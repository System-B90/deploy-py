"""Thin wrappers over the `docker` CLI.

`SB90_DOCKER` overrides the executable — a path, or a `.py` stub run with this
interpreter — which is how the tests drive the real flows without a daemon, on
Windows as well as Linux.
"""

import json
import os
import subprocess
import sys

from .console import Failure


def docker_argv():
    override = os.environ.get("SB90_DOCKER")
    if not override:
        return ["docker"]
    if override.endswith(".py"):
        return [sys.executable, override]
    return [override]


def run(argv, check=True, capture=False, env=None, cwd=None, quiet=False):
    """subprocess.run spelled for 3.6 (no capture_output/text keywords)."""
    kwargs = {"cwd": cwd, "universal_newlines": True}
    if env:
        merged = dict(os.environ)
        merged.update(env)
        kwargs["env"] = merged
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    elif quiet:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        result = subprocess.run(argv, check=False, **kwargs)
    except OSError as error:
        if check:
            raise Failure(f"Could not run {argv[0]}: {error}") from error
        return subprocess.CompletedProcess(argv, 127, "", str(error))
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, argv, result.stdout, result.stderr
        )
    return result


def docker(*args, **kwargs):
    return run(docker_argv() + list(args), **kwargs)


def succeeds(*args):
    return docker(*args, check=False, quiet=True).returncode == 0


def preflight():
    """The three checks that used to surface as opaque errors minutes into an install."""
    if not succeeds("--version"):
        raise Failure(
            "Docker is not installed or not in PATH.",
            "Install Docker Engine or Docker Desktop, then re-run.",
        )
    if not succeeds("info"):
        raise Failure(
            "Docker is installed but the daemon is not responding.",
            "Start Docker Desktop (or 'sudo systemctl start docker') and re-run.",
        )
    if not succeeds("compose", "version"):
        raise Failure(
            "'docker compose' (v2) is not available.",
            "This bundle needs Compose v2. The standalone 'docker-compose' v1 binary",
            "is not supported — upgrade Docker, or install the compose plugin.",
        )


def load_image(archive):
    """`docker load` one archive; returns the references it reported loading."""
    result = docker("load", "-i", archive, check=False, capture=True)
    if result.returncode != 0:
        raise Failure(
            f"docker load failed for {os.path.basename(archive)}.",
            (result.stderr or "").strip(),
        )
    loaded = []
    for line in (result.stdout or "").splitlines():
        if line.startswith("Loaded image:"):
            loaded.append(line.split(":", 1)[1].strip())
    return loaded


def image_exists(reference):
    return succeeds("image", "inspect", reference)


def container_running(name):
    result = docker("ps", "--format", "{{.Names}}", check=False, capture=True)
    return name in (result.stdout or "").split()


def container_label(name, label):
    template = '{{index .Config.Labels "' + label + '"}}'
    result = docker("inspect", name, "--format", template, check=False, capture=True)
    value = (result.stdout or "").strip() if result.returncode == 0 else ""
    return "" if value == "<no value>" else value


class Compose:
    """`docker compose -f ... --env-file ...` bound to one deployment."""

    def __init__(self, files, env_file, extra_env=None):
        self.files = list(files)
        self.env_file = env_file
        self.extra_env = dict(extra_env or {})

    def argv(self, *args):
        argv = docker_argv() + ["compose"]
        for path in self.files:
            argv += ["-f", path]
        argv += ["--env-file", self.env_file]
        return argv + list(args)

    def describe(self, *args):
        parts = ["docker compose"] + ["-f " + f for f in self.files] + list(args)
        return " ".join(parts)

    def run(self, *args, **kwargs):
        env = dict(self.extra_env)
        env.update(kwargs.pop("env", None) or {})
        return run(self.argv(*args), env=env, **kwargs)

    def ok(self, *args, **kwargs):
        kwargs.setdefault("check", False)
        return self.run(*args, **kwargs).returncode == 0

    def running_count(self):
        result = self.run(
            "ps", "--status", "running", "--quiet", check=False, capture=True
        )
        return len(
            [line for line in (result.stdout or "").splitlines() if line.strip()]
        )

    def service_image(self, service):
        result = self.run(
            "ps", "--format", "{{.Image}}", service, check=False, capture=True
        )
        lines = [line for line in (result.stdout or "").splitlines() if line.strip()]
        return lines[-1].strip() if lines else ""

    def exec_node(self, service, script):
        """Run a node one-liner in `service`; returns (ok, parsed JSON or raw text)."""
        result = self.run(
            "exec", "-T", service, "node", "-e", script, check=False, capture=True
        )
        body = (result.stdout or "").strip()
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = body
        return result.returncode == 0, parsed
