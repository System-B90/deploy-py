#!/usr/bin/env python3
"""Bundle bootstrapper: host Python 3.6+ -> a modern .venv -> sb90_deploy.

Shipped at the root of every release bundle by `sb90_deploy bundle`, and run by
the install.sh / install.ps1 (update, link-hive) shims:

    python3 bootstrap.py install|update|link-hive|setup [args...]

The only thing a deployment host is promised is Python 3.6+, so THIS FILE must
stay 3.6-compatible and stdlib-only. Everything else runs inside `.venv`, which
needs the oldest Python the bundle vendored wheels for (app.json `bundle.pythons`;
3.10, Ubuntu 22.04's stock python3, by default). The venv's interpreter is found on the
host - this interpreter if new enough, else `py -3.x` / `python3.1x` - and the
venv is (re)built whenever it is missing, too old, or out of date with the
bundle's wheels.
"""

import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
WHEELS = os.path.join(HERE, "wheels")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
STAMP = os.path.join(VENV, ".sb90-stamp")
INDEX = "https://system-b90.github.io/.github/pypi/"
# min(bundle.DEFAULT_PYTHONS): the floor when app.json names no pythons.
DEFAULT_FLOOR = (3, 10)
_PYTHON_RE = re.compile(r"^(\d+)\.(\d+)$")


def bundle_pythons():
    """The (major, minor) Pythons the bundle's wheels were vendored for.

    None when the bundle doesn't say (no app.json, or no bundle.pythons):
    older bundles, which keep DEFAULT_FLOOR. A present but unusable value is
    a bundle bug and raises ValueError rather than quietly falling back to
    the old floor - that fallback is the failure this guards against.
    """
    try:
        with open(os.path.join(HERE, "app.json"), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError("app.json is not valid JSON") from None
    bundle = data.get("bundle") if isinstance(data, dict) else None
    if not isinstance(bundle, dict) or "pythons" not in bundle:
        return None
    pythons = bundle["pythons"]
    if not isinstance(pythons, list) or not pythons:
        raise ValueError("app.json bundle.pythons must be a non-empty list")
    parsed = []
    for value in pythons:
        match = _PYTHON_RE.match(value) if isinstance(value, str) else None
        if not match:
            raise ValueError(
                'app.json bundle.pythons has %r; expected versions like "3.12"'
                % (value,)
            )
        parsed.append((int(match.group(1)), int(match.group(2))))
    return sorted(set(parsed))


try:
    BUNDLE_PYTHONS = bundle_pythons()
    BUNDLE_PYTHONS_ERROR = None
except ValueError as error:
    BUNDLE_PYTHONS, BUNDLE_PYTHONS_ERROR = None, str(error)
VENV_FLOOR = BUNDLE_PYTHONS[0] if BUNDLE_PYTHONS else DEFAULT_FLOOR


def exact_pythons():
    """The only Pythons a venv may use, or None when any above the floor will do.

    An offline bundle installs from its own wheels with --no-index, and those
    are built per Python version: a newer interpreter passes a floor check,
    then finds no cp3XX wheels. An online bundle installs from the index, so
    the floor is enough there.
    """
    if BUNDLE_PYTHONS and os.path.isdir(WHEELS):
        return BUNDLE_PYTHONS
    return None


def usable(version):
    if not version:
        return False
    exact = exact_pythons()
    return version in exact if exact else version >= VENV_FLOOR


def needed_text():
    exact = exact_pythons()
    if exact:
        return " or ".join("%d.%d" % v for v in exact)
    return "%d.%d+" % VENV_FLOOR


def fail(message, *hints):
    sys.stderr.write("\n[ERROR] %s\n" % message)
    for hint in hints:
        sys.stderr.write("        %s\n" % hint)
    sys.exit(1)


def version_of(argv):
    try:
        out = subprocess.check_output(
            argv + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
        major, minor = out.strip().split(".")
        return int(major), int(minor)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def candidates():
    yield [sys.executable]
    minors = range(20, VENV_FLOOR[1] - 1, -1)
    if os.name == "nt":
        for minor in minors:
            yield ["py", "-3.%d" % minor]
        yield ["python"]
    else:
        for minor in minors:
            yield ["python3.%d" % minor]
        yield ["python3"]


def find_interpreter():
    for argv in candidates():
        if usable(version_of(argv)):
            return argv
    exact = exact_pythons()
    fail(
        "Python %s is needed for the deployment tools, but it was not found."
        % needed_text()
        if exact
        else "Python %s is needed for the deployment tools, but only older versions were found."
        % needed_text(),
        "This bundle has wheels for exactly those versions."
        if exact
        else "This host's Python (%d.%d) is enough to start, not to run them."
        % sys.version_info[:2],
        "Install Python %s alongside it (Windows: python.org), then re-run."
        % needed_text(),
        "It is found automatically; nothing else needs to change.",
    )


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def bundle_stamp():
    """Changes whenever the bundle's install inputs do (an upgrade swaps them)."""
    parts = []
    for path in sorted(glob.glob(os.path.join(WHEELS, "*.whl"))) + [REQUIREMENTS]:
        if os.path.exists(path):
            parts.append("%s:%d" % (os.path.basename(path), os.path.getsize(path)))
    return "\n".join(parts)


def create_venv(interpreter):
    """`python -m venv`, surviving Debian/Ubuntu's stripped ensurepip.

    Ubuntu ships the venv module but removes ensurepip unless python3-venv is
    apt-installed - which an air-gapped box often cannot do. So on failure the
    venv is made --without-pip and pip is installed from the vendored pip
    wheel (a wheel is importable as-is, so pip can install itself from it).
    """
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if subprocess.call(interpreter + ["-m", "venv", "--clear", VENV], **quiet) == 0:
        return
    print(
        "[WAIT] ensurepip unavailable (no python3-venv); using the bundled pip wheel..."
    )
    pip_wheels = sorted(glob.glob(os.path.join(WHEELS, "pip-*.whl")))
    if (
        pip_wheels
        and subprocess.call(
            interpreter + ["-m", "venv", "--clear", "--without-pip", VENV]
        )
        == 0
    ):
        env = dict(os.environ, PYTHONPATH=pip_wheels[-1])
        code = subprocess.call(
            [
                venv_python(),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-index",
                pip_wheels[-1],
            ],
            env=env,
        )
        if code == 0:
            return
    fail(
        "Could not create a Python virtualenv in %s." % VENV,
        "On Ubuntu: sudo apt install python3-venv",
    )


def ensure_venv(force=False):
    python = venv_python()
    current = version_of([python]) if os.path.exists(python) else None
    stamp = bundle_stamp()
    if not force and usable(current) and os.path.exists(STAMP):
        with open(STAMP) as handle:
            if handle.read() == stamp:
                return python

    if not usable(current):
        interpreter = find_interpreter()
        print("[WAIT] Creating .venv with %s..." % " ".join(interpreter))
        create_venv(interpreter)

    pip = [python, "-m", "pip", "install", "--upgrade", "--quiet"]
    if os.path.isdir(WHEELS):
        # The offline bundle vendors every wheel; --no-index keeps an
        # air-gapped box from reaching for PyPI and the org index.
        pip += ["--no-index", "--find-links", WHEELS]
    else:
        pip += ["--extra-index-url", INDEX]
    print("[WAIT] Installing deployment tools into .venv...")
    if subprocess.call(pip + ["-r", REQUIREMENTS]) != 0:
        fail(
            "Could not install the deployment tools into .venv.",
            "Offline bundle: it may be incomplete - re-download it."
            if os.path.isdir(WHEELS)
            else "Online bundle: check this host can reach PyPI and %s" % INDEX,
        )
    with open(STAMP, "w") as handle:
        handle.write(stamp)
    return python


def main(argv):
    if sys.version_info < (3, 6):
        fail(
            "Python 3.6+ is required to run the installer (found %d.%d)."
            % sys.version_info[:2]
        )
    if not argv or argv == ["--reinstall-venv"]:
        fail("usage: bootstrap.py install|update|link-hive|setup [args...]")
    if BUNDLE_PYTHONS_ERROR:
        fail(
            "This bundle is broken: %s." % BUNDLE_PYTHONS_ERROR,
            "Re-download the bundle; if it persists, report it with the bundle name.",
        )
    python = ensure_venv(force="--reinstall-venv" in argv)
    if argv[0] == "--venv-only":
        return
    argv = [a for a in argv if a != "--reinstall-venv"]
    command = [python, "-m", "sb90_deploy", argv[0], "--root", HERE] + argv[1:]
    if os.name == "nt":
        sys.exit(subprocess.call(command))
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(python, command)


if __name__ == "__main__":
    main(sys.argv[1:])
