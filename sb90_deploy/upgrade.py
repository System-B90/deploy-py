"""In-place release upgrade — online (pull) or offline (--package). Was Bluz's update.sh.

Back up, get the new images, refresh the bundle's own files, roll the containers
one service at a time, let the ui image run its own migrations, then verify. Any
failed step stops the upgrade and prints the exact rollback command, including
the backup taken at the start of this run.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

from . import docker, envfile
from .console import Failure, confirm, log, ok, report, say, warn
from .deployment import Deployment
from .spec import (
    COMPOSE_FILE,
    VERSION_FILE,
    compose_images,
    file_hashes,
    read_manifest,
)

# Swapped wholesale rather than file by file.
_DIRECTORIES = ("wheels",)
# Never copied from a package into a live deployment.
_ALWAYS_STATE = (".env", ".venv", "images")

HEALTH_SCRIPT = (
    "fetch(%s).then(r=>r.text().then(t=>{console.log(t);process.exit(r.ok?0:1)}))"
    ".catch(()=>process.exit(1))"
)


class Upgrade:
    def __init__(self, deployment: Deployment, args):
        self.d = deployment
        self.spec = deployment.spec
        self.args = args
        self.previous = ""
        self.target = args.version or ""
        self.package_root = ""
        self.scratch: list[str] = []
        self.backup_dir = ""
        self.bundle_backup = ""
        self.compose = deployment.compose()

    # -- failure path --------------------------------------------------------

    def abort(self, step: str) -> Failure:
        """Every failure past the point of no return: which step, and how back."""
        hints = []
        if self.previous:
            hints += [
                "The stack is left as-is for inspection. To roll the images back:",
                f"  1. set {self.spec.version_var}={self.previous} in {self.d.env_file}",
                f"  2. {self.compose.describe('up', '-d', '--wait')}",
            ]
            if self.args.package:
                # `docker load` never removes the tags it replaces.
                hints.append(
                    f"The {self.previous} images are still loaded locally - no download needed."
                )
        if self.bundle_backup:
            hints += [
                "The bundle's own files were replaced. The previous copies are in:",
                f"  {self.bundle_backup}",
            ]
        if self.backup_dir:
            restore = self._backup_script("restore_")
            hints += [
                "A migration may have already changed the databases. To restore this run's backup:",
                f"  {restore or 'the restore script'} {self.backup_dir}",
            ]
        return Failure(f"Upgrade failed during: {step}", *hints)

    # -- target resolution ---------------------------------------------------

    def _extract(self, archive: str) -> str:
        target = tempfile.mkdtemp(prefix=f"{self.spec.name}-package-")
        self.scratch.append(target)
        with tarfile.open(archive) as tar:
            root = os.path.realpath(target)
            for member in tar.getmembers():
                dest = os.path.realpath(os.path.join(target, member.name))
                if not dest.startswith(root + os.sep) and dest != root:
                    raise Failure(f"{archive} contains an unsafe path: {member.name}")
            tar.extractall(target)
        return target

    def _descend(self, root: str) -> str:
        """The tarball wraps the bundle in one directory; step into it."""
        if os.path.isfile(os.path.join(root, COMPOSE_FILE)):
            return root
        nested = [p for p in glob.glob(os.path.join(root, "*")) if os.path.isdir(p)]
        if len(nested) == 1 and os.path.isfile(os.path.join(nested[0], COMPOSE_FILE)):
            return nested[0]
        return root

    def _manifest_tag(self, root: str) -> str:
        """Older offline bundles had no VERSION; read the own image's tag from its archive."""
        for archive in sorted(
            glob.glob(os.path.join(root, "images", f"{self.spec.name}-*.tar"))
        ):
            try:
                with tarfile.open(archive) as tar:
                    manifest = json.load(tar.extractfile("manifest.json"))
                for entry in manifest:
                    for tag in entry.get("RepoTags") or []:
                        return tag.rsplit(":", 1)[1]
            except (KeyError, OSError, ValueError, tarfile.TarError):
                continue
        return ""

    def resolve_package(self) -> None:
        path = self.args.package
        if os.path.isdir(path):
            root = os.path.abspath(path)
        elif os.path.isfile(path):
            log(self.spec.name, f"extracting {path}...")
            try:
                root = self._extract(path)
            except tarfile.TarError as error:
                raise Failure(
                    f"Could not extract {path}: {error}",
                    f"Expected the release's {self.spec.offline_archive('<tag>')}.",
                ) from error
        else:
            raise Failure(
                f"Package not found: {path}",
                f"Pass the {self.spec.offline_archive('<tag>')} you downloaded, or the",
                "directory it extracts into.",
            )
        root = self._descend(root)
        if not os.path.isfile(os.path.join(root, COMPOSE_FILE)):
            raise Failure(
                f"No {COMPOSE_FILE} in the package ({root}).",
                f"That is not a {self.spec.display_name} release bundle.",
            )
        if not glob.glob(os.path.join(root, "images", "*.tar")):
            raise Failure(
                f"No images/*.tar in the package ({root}).",
                "This is the ONLINE bundle - it carries no images and cannot",
                f"upgrade an air-gapped host. Download {self.spec.offline_archive('<tag>')}.",
            )
        version_file = os.path.join(root, VERSION_FILE)
        if os.path.isfile(version_file):
            with open(version_file, encoding="utf-8") as handle:
                self.target = handle.read().strip()
        else:
            self.target = self._manifest_tag(root)
        if not self.target:
            raise Failure(
                "Could not tell which release the package holds.",
                "It has no VERSION file and no readable tag in its image archives.",
                f"Write one and re-run: echo v1.0.0 > {version_file}",
            )
        self.package_root = root
        ok(f"offline package: {self.target} ({root})")

    def _github(self, url: str) -> object:
        request = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def resolve_latest(self) -> None:
        """Newest published release (Bluz#479); /releases/latest hides prereleases."""
        repo = self.spec.setting("RELEASE_REPO", self.spec.release_repo)
        api = f"https://api.github.com/repos/{repo}/releases"
        try:
            if self.args.pre_release:
                log(
                    self.spec.name,
                    f"resolving latest release (including prereleases) from {repo}...",
                )
                data = self._github(api + "?per_page=1")
                self.target = data[0]["tag_name"] if data else ""
            else:
                log(self.spec.name, f"resolving latest release from {repo}...")
                self.target = self._github(api + "/latest")["tag_name"]
        except (OSError, ValueError, KeyError, IndexError):
            self.target = ""
        if self.target:
            ok(f"latest release is {self.target}")
            return
        warn("could not reach GitHub - falling back to the bundle's VERSION file")
        self.target = self.d.bundle_version()
        if not self.target:
            raise Failure(
                "No target version given, GitHub unreachable, and no VERSION file.",
                "Pass it explicitly: ./update.sh --version v1.0.0",
                "Air-gapped host? Upgrade from a package: ./update.sh --package <path>",
            )

    # -- steps ---------------------------------------------------------------

    def _backup_script(self, prefix: str = "") -> str:
        if not self.spec.backup:
            return ""
        override = self.spec.setting("BACKUP_SCRIPT") if not prefix else None
        if override:
            return override
        key = prefix + ("ps1" if os.name == "nt" else "sh")
        relative = self.spec.backup.get(key)
        path = os.path.join(self.d.root, relative) if relative else ""
        return path if path and os.path.isfile(path) else ""

    def run_backup(self) -> None:
        script = self._backup_script()
        log(self.spec.name, "backing up...")
        env = dict(os.environ)
        env[self.spec.env_name("COMPOSE_FILE")] = self.d.compose_file
        env[self.spec.env_name("ENV_FILE")] = self.d.env_file
        argv = (
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script]
            if script.endswith(".ps1")
            else ["bash", script]
        )
        if subprocess.call(argv, env=env) != 0:
            raise self.abort("backup")
        root_key = "root_windows" if os.name == "nt" else "root"
        default_root = os.path.expandvars(self.spec.backup.get(root_key, ""))
        backup_root = self.spec.setting("BACKUP_DIR", default_root)
        if backup_root and os.path.isdir(backup_root):
            dumps = sorted(glob.glob(os.path.join(backup_root, "*", "*")))
            dumps = [p for p in dumps if os.path.isdir(p)]
            self.backup_dir = dumps[-1] if dumps else ""
        ok(f"backup written to {self.backup_dir or backup_root}")

    def fetch_images(self) -> None:
        if self.args.package:
            log(
                self.spec.name,
                f"loading {self.target} images from the package (containers keep running)...",
            )
            # Every archive, not just ours: a release may move the DB pins and
            # the new tag then exists nowhere on an air-gapped host but here.
            for archive in sorted(
                glob.glob(os.path.join(self.package_root, "images", "*.tar"))
            ):
                log(self.spec.name, f"  {os.path.basename(archive)}")
                try:
                    docker.load_image(archive)
                except Failure as error:
                    raise self.abort(f"loading {os.path.basename(archive)}") from error
            ok("images loaded")
            with open(
                os.path.join(self.package_root, COMPOSE_FILE), encoding="utf-8"
            ) as handle:
                own = [
                    r
                    for r, mine in compose_images(
                        handle.read(), self.spec.version_var, self.target
                    )
                    if mine
                ]
            for reference in own:
                if not docker.image_exists(reference):
                    raise self.abort(
                        f"package verification: {reference} is not among the loaded images"
                    )
            ok(
                f"all {len(own)} {self.spec.display_name} images present at {self.target}"
            )
            return

        log(
            self.spec.name, f"pulling {self.target} images (containers keep running)..."
        )
        if not self.compose.ok("pull", env={self.spec.version_var: self.target}):
            raise self.abort("image pull")
        ok("images pulled")
        # Online upgrades used to refresh only images, leaving setup.py and the
        # scripts at whatever first installed the host forever. Every release
        # publishes an online bundle with the same files as the offline one.
        repo = self.spec.setting("RELEASE_REPO", self.spec.release_repo)
        name = self.spec.online_archive(self.target)
        url = f"https://github.com/{repo}/releases/download/{self.target}/{name}"
        log(self.spec.name, f"fetching {name}...")
        scratch = tempfile.mkdtemp(prefix=f"{self.spec.name}-online-")
        self.scratch.append(scratch)
        archive = os.path.join(scratch, name)
        try:
            with (
                urllib.request.urlopen(url, timeout=60) as response,
                open(archive, "wb") as out,
            ):
                shutil.copyfileobj(response, out)
            self.package_root = self._descend(self._extract(archive))
        except (OSError, tarfile.TarError, Failure) as error:
            raise self.abort(f"downloading {name}") from error
        if not os.path.isfile(os.path.join(self.package_root, "setup.py")):
            raise self.abort(f"{name} did not contain the expected bundle layout")

    def _is_state(self, relative: str) -> bool:
        norm = relative.replace(os.sep, "/")
        for keep in list(_ALWAYS_STATE) + list(self.spec.state):
            if norm == keep or norm.startswith(keep.rstrip("/") + "/"):
                return True
        return norm.startswith(".env")

    def local_edits(self) -> list[str] | None:
        """Bundle files edited on this host since install, that the package replaces.

        None when the deployment predates the manifest and edits cannot be told
        apart from the previous release's own content.
        """
        shipped = read_manifest(self.d.root)
        if shipped is None:
            return None
        incoming = file_hashes(self.package_root)
        live = file_hashes(self.d.root)
        return sorted(
            relative
            for relative, digest in shipped.items()
            if relative in incoming
            and relative in live
            and live[relative] != digest
            and live[relative] != incoming[relative]
            and not self._is_state(relative)
        )

    def guard_local_edits(self) -> None:
        """Hand edits to bundle files (e.g. an extra network in the hive-local
        overlay) were silently replaced, taking a co-located prod down."""
        edits = self.local_edits()
        backup = f".bundle-bak-{self.previous}"
        if edits is None:
            warn("this deployment predates the bundle manifest - any hand edits to its")
            warn(
                f"compose files or scripts will be replaced (previous copies -> {backup})"
            )
            return
        if not edits:
            return
        warn("these bundle files were edited on this host and will be REPLACED:")
        for relative in edits:
            say(f"        {relative}")
        say(f"        previous copies are kept in {backup}/ - re-apply what is still")
        say("        needed after the upgrade, ideally as a compose override file.")
        if not confirm(
            "Replace them and continue?", default=False, assume_yes=self.args.yes
        ):
            raise Failure(
                "Upgrade stopped before replacing locally edited files.",
                "Nothing was rolled; the running containers and .env are untouched.",
                "Re-run with --yes to replace them anyway.",
            )

    def refresh_bundle(self) -> None:
        """Replace the deployment's scripts/compose/tools with the package's.

        Host-specific state (.env, certificates, app.json `state` entries) is
        never touched. Previous copies go to .bundle-bak-<previous>.
        """
        self.guard_local_edits()
        self.bundle_backup = os.path.join(self.d.root, f".bundle-bak-{self.previous}")
        log(
            self.spec.name,
            f"refreshing bundle files (previous copies -> {self.bundle_backup})...",
        )
        os.makedirs(self.bundle_backup, exist_ok=True)
        for directory, subdirs, files in os.walk(self.package_root):
            rel_dir = os.path.relpath(directory, self.package_root)
            rel_dir = "" if rel_dir == "." else rel_dir
            subdirs[:] = [
                s
                for s in subdirs
                if s not in _DIRECTORIES
                and not self._is_state(os.path.join(rel_dir, s))
                and s != "__pycache__"
            ]
            for name in files:
                relative = os.path.join(rel_dir, name)
                if self._is_state(relative):
                    continue
                source = os.path.join(directory, name)
                live = os.path.join(self.d.root, relative)
                try:
                    if os.path.isfile(live):
                        saved = os.path.join(self.bundle_backup, relative)
                        os.makedirs(os.path.dirname(saved), exist_ok=True)
                        shutil.copy2(live, saved)
                    os.makedirs(os.path.dirname(live), exist_ok=True)
                    # New inode, never an in-place overwrite of a running script.
                    shutil.copy2(source, live + ".new")
                    os.replace(live + ".new", live)
                except OSError as error:
                    raise self.abort(f"installing {relative}") from error
        for name in _DIRECTORIES:
            source = os.path.join(self.package_root, name)
            if not os.path.isdir(source):
                continue
            live = os.path.join(self.d.root, name)
            try:
                if os.path.isdir(live):
                    shutil.move(live, os.path.join(self.bundle_backup, name))
                shutil.copytree(source, live)
            except OSError as error:
                raise self.abort(f"installing {name}/") from error
        ok("bundle files refreshed")
        # .venv is rebuilt from the new wheels by bootstrap.py on its next run
        # (it stamps what it installed); nudge it now so tools match (Bluz#672).
        bootstrap = os.path.join(self.d.root, "bootstrap.py")
        if os.path.isfile(bootstrap):
            log(self.spec.name, "refreshing .venv from the new bundle...")
            code = subprocess.call([sys.executable, bootstrap, "--venv-only"])
            if code != 0:
                warn(
                    "could not refresh .venv - it will be rebuilt on the next ./update.sh run"
                )

    def roll(self) -> None:
        """Service by service, each healthy before the next. ui first (it runs
        the migrations), proxy last (the public endpoint blinks last)."""
        shutil.copy2(self.d.env_file, f"{self.d.env_file}.bak-{self.previous}")
        envfile.set_value(self.d.env_file, self.spec.version_var, self.target)
        # A real env var outranks --env-file in compose substitution; pin it.
        self.compose.extra_env[self.spec.version_var] = self.target
        for service in self.spec.roll:
            log(self.spec.name, f"rolling {service}...")
            # --no-deps keeps the databases from being recreated as a side effect.
            if not self.compose.ok("up", "-d", "--wait", "--no-deps", service):
                raise self.abort(f"rolling {service}")
            running = self.compose.service_image(service)
            if running and ":" in running and not running.endswith(":" + self.target):
                raise self.abort(
                    f"post-roll verification: {service} is running {running}, expected {self.target}"
                )
            ok(f"{service} is up")

    def verify(self) -> None:
        log(self.spec.name, f"verifying {self.spec.health_url}...")
        retries = int(self.spec.setting("HEALTH_RETRIES", "30"))
        script = HEALTH_SCRIPT % json.dumps(self.spec.health_url)
        body = None
        for _attempt in range(retries):
            healthy, body = self.compose.exec_node(self.spec.health_service, script)
            if healthy:
                break
            time.sleep(2)
        else:
            raise self.abort("health check")
        # A 200 can still be "degraded" (e.g. Hive unreachable) - name it.
        if isinstance(body, dict):
            if body.get("checks"):
                say("        " + json.dumps(body["checks"]))
            if body.get("status") == "unhealthy":
                raise self.abort("health check reported unhealthy")
        ok("health endpoint is green")

    # -- driver --------------------------------------------------------------

    def run(self) -> int:
        spec, args = self.spec, self.args
        if args.package and (args.version or args.pre_release):
            raise Failure(
                "--package cannot be combined with --version / --pre-release.",
                "An offline package IS the version; it carries exactly one release.",
            )
        docker.preflight()
        self.d.require_compose_file()
        if not os.path.isfile(self.d.env_file):
            raise Failure(
                f"Env file not found: {self.d.env_file}",
                "An upgrade reuses the existing deployment's .env - this looks like",
                "a fresh host. Run ./install.sh instead.",
            )
        if self.d.uses_overlay():
            log(
                spec.name,
                "co-located Hive detected (HIVE_NETWORK_NAME in .env) - using the overlay",
            )
        self.previous = self.d.env().get(spec.version_var, "")
        if not self.previous:
            raise Failure(
                f"{spec.version_var} is not set in {self.d.env_file}.",
                "Set it to the running release first, so there is a version to roll back to.",
            )

        if args.package:
            self.resolve_package()
        elif not self.target:
            self.resolve_latest()
        if self.target == self.previous:
            raise Failure(
                f"Already running {self.previous}.",
                "Pass a different --version, or nothing needs doing.",
            )
        if self.compose.running_count() == 0:
            raise Failure(
                f"No {spec.display_name} containers are running.",
                "This upgrades a live deployment. Start it first:",
                f"  {self.compose.describe('up', '-d', '--wait')}",
            )
        do_backup = bool(spec.backup) and not args.skip_backup
        if do_backup and not self._backup_script():
            raise Failure(
                "Could not find the backup script.",
                "An upgrade runs migrations, which are not reversible without a dump.",
                f"Point at it with {spec.env_name('BACKUP_SCRIPT')}=<path>,",
                "or accept the risk explicitly with --skip-backup.",
            )

        say()
        log(spec.name, f"upgrade {self.previous} -> {self.target}")
        log(
            spec.name,
            "mode         : "
            + (f"offline ({args.package})" if args.package else "online"),
        )
        log(spec.name, f"install dir  : {self.d.root}")
        log(spec.name, "compose files: " + ", ".join(self.d.compose_files()))
        if spec.backup:
            log(
                spec.name,
                "backup       : "
                + (
                    "DISABLED (--skip-backup)"
                    if not do_backup
                    else self._backup_script()
                ),
            )
        say()
        if not confirm("Proceed?", default=False, assume_yes=args.yes):
            say("Aborted.")
            return 1

        if do_backup:
            self.run_backup()
        elif spec.backup:
            warn(
                "skipping backup (--skip-backup): a failed migration will not be recoverable"
            )
        self.fetch_images()
        self.refresh_bundle()
        self.roll()
        self.verify()

        say()
        ok(f"{spec.display_name} upgraded {self.previous} -> {self.target}")
        say(f"        previous env file kept at {self.d.env_file}.bak-{self.previous}")
        say(f"        previous bundle files: {self.bundle_backup}")
        if self.backup_dir:
            say(f"        pre-upgrade backup: {self.backup_dir}")
        return 0

    def cleanup(self) -> None:
        for path in self.scratch:
            shutil.rmtree(path, ignore_errors=True)


def upgrade(deployment: Deployment, args) -> int:
    job = Upgrade(deployment, args)
    try:
        return job.run()
    finally:
        job.cleanup()


__all__ = ["Upgrade", "report", "upgrade"]
