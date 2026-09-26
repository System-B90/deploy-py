"""Crafting an app's online + offline release bundles (was each repo's craft-release job).

    python -m sb90_deploy bundle --app deploy/app.json --tag v1.2.3 --out dist

Both bundles extract into a versionless `<bundle.dir>/` — upgrades are in place,
so a version in the directory name would be a lie after the first upgrade — and
carry the same files, so an online upgrade can refresh a deployment's scripts
from `<dir>-online-<tag>.tar.gz` exactly as an offline one does from its package:

    app.json, VERSION, requirements.txt, bootstrap.py
    install.sh/.ps1, update.sh/.ps1 (+ link-hive.sh/.ps1 with a Hive overlay)
    every entry of app.json `bundle.files` (compose files, setup.py, backups...)

The offline bundle adds images/*.tar (every image the release compose file
names, resolved for the tag — read from the file so the tars and an online pull
cannot disagree) and wheels/ (sb90-deploy, pip, and `venv_packages`, for every
target platform and Python the bootstrap may find).

`app.json` bundle keys:
    dir           archive/directory name (default: name)
    files         {"bundle/path": "repo/path", ...}
    local_wheels  globs of wheels built earlier in the job (e.g. cli/dist/*.whl)
    platforms     wheel platforms (default manylinux2014_x86_64, win_amd64)
    pythons       Python versions to vendor for (default 3.10 3.11 3.12 3.13)
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tarfile
from importlib import resources
from pathlib import Path

from . import __version__
from .console import Failure, log, ok, warn
from .spec import (
    COMPOSE_FILE,
    OVERLAY_FILE,
    SETUP_SCRIPT,
    SPEC_FILE,
    VERSION_FILE,
    AppSpec,
    compose_images,
    image_archive_name,
)

DEFAULT_PLATFORMS = ["manylinux2014_x86_64", "win_amd64"]
# 3.10 is Ubuntu 22.04's stock python3 - the most common target.
DEFAULT_PYTHONS = ["3.10", "3.11", "3.12", "3.13"]


def _template(name: str) -> str:
    text = resources.files("sb90_deploy").joinpath("templates", name).read_text(encoding="utf-8")
    # A wheel built on Windows may carry CRLF; a shebang script must not.
    return text.replace("\r\n", "\n")


def _write(path: Path, text: str, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o755)


def requirements(spec: AppSpec) -> str:
    lines = [
        "# Installed into the bundle's .venv by bootstrap.py.",
        f"sb90-deploy=={__version__}",
    ] + spec.venv_packages
    return "\n".join(lines) + "\n"


def populate(stage: Path, spec: AppSpec, spec_path: Path, repo: Path, tag: str) -> None:
    for target, source in spec.bundle.get("files", {}).items():
        origin = repo / source
        if not origin.exists():
            raise Failure(f"bundle input missing: {source}")
        destination = stage / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination)
        else:
            shutil.copy2(origin, destination)
    shutil.copy2(spec_path, stage / SPEC_FILE)
    _write(stage / VERSION_FILE, tag + "\n")
    _write(stage / "requirements.txt", requirements(spec))
    _write(stage / "bootstrap.py", _template("bootstrap.py"), executable=True)
    commands = ["install", "update"] + (["link-hive"] if (stage / OVERLAY_FILE).exists() else [])
    for command in commands:
        _write(stage / f"{command}.sh", _template("shim.sh"), executable=True)
        _write(stage / f"{command}.ps1", _template("shim.ps1"))
    for script in stage.rglob("*.sh"):
        script.chmod(0o755)


def guard(stage: Path) -> None:
    """Fail loudly on the mistakes that shipped broken bundles before (Bluz#451, #452)."""
    compose = stage / COMPOSE_FILE
    if not compose.exists():
        raise Failure(
            f"{stage.name}: no {COMPOSE_FILE} (map deploy/docker-compose.release.yml to it)"
        )
    if "../" in compose.read_text(encoding="utf-8"):
        raise Failure(f"{stage.name}: {COMPOSE_FILE} references ../ - that is the dev compose file")
    if not (stage / SETUP_SCRIPT).exists():
        raise Failure(f"{stage.name}: no {SETUP_SCRIPT} in the bundle")


def save_images(stage: Path, spec: AppSpec, tag: str, mode: str) -> list[str]:
    text = (stage / COMPOSE_FILE).read_text(encoding="utf-8")
    images = compose_images(text, spec.version_var, tag)
    if mode == "skip":
        warn("images skipped - the offline bundle is not installable as-is")
        return []
    target = stage / "images"
    target.mkdir(exist_ok=True)
    saved = []
    for reference, own in images:
        if mode == "pull":
            subprocess.run(["docker", "pull", reference], check=True)
        archive = target / image_archive_name(reference, own, spec.name)
        log("bundle", f"saving {reference} -> images/{archive.name}")
        subprocess.run(["docker", "save", reference, "-o", str(archive)], check=True)
        saved.append(reference)
    return saved


# pip platform tag -> uv's --python-platform
UV_PLATFORMS = {
    "manylinux2014_x86_64": "x86_64-manylinux2014",
    "win_amd64": "x86_64-pc-windows-msvc",
}


def _extra_links() -> list[str]:
    """SB90_BUNDLE_FIND_LINKS: extra wheel dirs (os.pathsep-separated), e.g. a
    local mirror, or fresh wheels the Pages CDN is not serving yet."""
    extra = os.environ.get("SB90_BUNDLE_FIND_LINKS", "")
    links: list[str] = []
    for path in filter(None, extra.split(os.pathsep)):
        links += ["--find-links", path]
    return links


def _uv() -> str:
    from uv import find_uv_bin

    return find_uv_bin()


def lock_for(
    requirements_file: Path, platform: str, python: str, wheels: Path, index: str, out: Path
) -> Path:
    """Resolve the full dependency set *as the target host sees it*.

    `pip download --platform/--python-version` still evaluates environment
    markers against the machine running it, so bundling on 3.13 dropped
    `exceptiongroup; python_version < "3.11"` and bundling on Windows dropped
    `SecretStorage; sys_platform == "linux"` — both only visible when the
    air-gapped install failed. uv resolves for a named target environment.
    """
    subprocess.run(
        [
            _uv(),
            "pip",
            "compile",
            "--quiet",
            "--no-header",
            "--no-annotate",
            str(requirements_file),
            "--python-version",
            python,
            "--python-platform",
            UV_PLATFORMS.get(platform, platform),
            "--only-binary",
            ":all:",
            "--find-links",
            str(wheels),
            *_extra_links(),
            "--extra-index-url",
            index,
            "--index-strategy",
            "unsafe-best-match",
            "-o",
            str(out),
        ],
        check=True,
    )
    return out


def vendor_wheels(stage: Path, spec: AppSpec, repo: Path, requirements_file: Path) -> int:
    wheels = stage / "wheels"
    wheels.mkdir(exist_ok=True)
    local = []
    for pattern in spec.bundle.get("local_wheels", []):
        matches = glob.glob(str(repo / pattern))
        if not matches:
            raise Failure(f"local wheel not built: {pattern}")
        for match in matches:
            shutil.copy2(match, wheels)
            local.append(match)
    platforms = spec.bundle.get("platforms", DEFAULT_PLATFORMS)
    pythons = spec.bundle.get("pythons", DEFAULT_PYTHONS)
    # Strict on purpose: a dependency that stops publishing a wheel must fail
    # the release here, not on a customer's air-gapped machine.
    for platform in platforms:
        for python in pythons:
            log("bundle", f"vendoring wheels for {platform} / Python {python}...")
            lock = lock_for(
                requirements_file,
                platform,
                python,
                wheels,
                spec.pip_index,
                wheels.parent / f".lock-{platform}-{python}.txt",
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "download",
                    "--quiet",
                    "--no-deps",
                    "-r",
                    str(lock),
                    "pip",
                    "--only-binary=:all:",
                    "--platform",
                    platform,
                    "--python-version",
                    python,
                    "--find-links",
                    str(wheels),
                    *_extra_links(),
                    "--extra-index-url",
                    spec.pip_index,
                    "-d",
                    str(wheels),
                ],
                check=True,
            )
            lock.unlink()
    count = len(list(wheels.glob("*.whl")))
    if count <= len(local) + 1:
        raise Failure("offline bundle has no vendored wheels")
    return count


def _modes(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Exec bits set in the archive itself: chmod is a no-op when a bundle is
    crafted on Windows (sb90.py's offline path), and install.sh must still run."""
    if info.isfile():
        executable = info.name.endswith((".sh", "bootstrap.py"))
        info.mode = 0o755 if executable else 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack(stage_parent: Path, spec: AppSpec, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage_parent / spec.bundle_dir, arcname=spec.bundle_dir, filter=_modes)
    ok(f"packed {archive}")


def craft(
    spec_path: str,
    tag: str,
    out: str,
    repo: str | None = None,
    images: str = "pull",
    wheels: bool = True,
) -> list[Path]:
    spec_file = Path(spec_path).resolve()
    spec = AppSpec.load(str(spec_file))
    repo_root = Path(repo or os.getcwd()).resolve()
    out_dir = Path(out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / f".stage-{spec.name}"
    shutil.rmtree(work, ignore_errors=True)
    archives = []
    try:
        for flavour in ("online", "offline"):
            parent = work / flavour
            stage = parent / spec.bundle_dir
            stage.mkdir(parents=True)
            populate(stage, spec, spec_file, repo_root, tag)
            guard(stage)
            if flavour == "offline":
                save_images(stage, spec, tag, images)
                if wheels:
                    count = vendor_wheels(stage, spec, repo_root, stage / "requirements.txt")
                    ok(f"vendored {count} wheels")
                else:
                    warn("wheels skipped - the air-gapped install path will fail")
            archive = out_dir / (
                spec.online_archive(tag) if flavour == "online" else spec.offline_archive(tag)
            )
            pack(parent, spec, archive)
            archives.append(archive)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"online={archives[0]}\noffline={archives[1]}\n")
    return archives
