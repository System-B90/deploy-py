"""Delta packages: an offline upgrade that ships only what the host lacks.

    python -m sb90_deploy delta --target dist/<dir>-offline-v1.3.0.tar.gz \\
        --base <dir>-offline-v1.2.1.tar.gz --base <dir>-offline-v1.2.0.tar.gz --out dist

`<dir>-delta-<tag>.tar.zst` upgrades a host running ANY of the bases, in place,
with one file - like a cumulative Windows update. It holds:

    <dir>/<bundle files>        same as the online bundle (a few KB)
    <dir>/.delta/delta.json     what to rebuild, and from what
    <dir>/.delta/blobs/         image layers no base has
    <dir>/.delta/wheels/        wheels no base has

Image layers are where the size is. Per layer of the target release:

* every base has it (diffID match)        -> not shipped; the host has it loaded
* some base lacks it                      -> the smaller of
    - the layer, zstd --long --ultra -22
    - one zstd --patch-from per distinct base counterpart (the layer at the
      same position of the same image in that base): only the bytes that changed

The host (`update --package <delta>`) `docker save`s the images it is running,
rebuilds every target image archive from those layers + the patches, checks each
layer against its diffID, and continues as a normal offline upgrade. A host not
on a listed base is told to use the full offline package.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass, field

import zstandard as zstd

from .archive import Image, SavedImages, digest, safe_extract, write_image
from .bundle import _modes
from .console import Failure, log, ok, warn
from .spec import COMPOSE_FILE, SPEC_FILE, VERSION_FILE, AppSpec

FORMAT = 1
# The first sb90-deploy that applies deltas. A base bundled with an older one
# upgrades with its own (older) tools, which cannot: it needs the full package.
FIRST_TOOL = (0, 6, 0)
DELTA_DIR = ".delta"
DELTA_FILE = "delta.json"
# --ultra -22: its deep match search is what finds a layer's unchanged files in
# a base of 80 MB+ (Bluz ui layer v1.6.1 -> v1.6.2: 58 KB at 22, 9.4 MB at 19).
LEVEL = 22
# zstd's ceiling: a patch's base + target must fit in one window.
MAX_WINDOW_LOG = 31


def _window_log(size: int) -> int:
    return max(20, min(MAX_WINDOW_LOG, (max(size, 1) - 1).bit_length()))


def compress(data: bytes, base: bytes | None = None) -> bytes:
    """zstd --long --ultra -22, or --patch-from=base when given."""
    params = zstd.ZstdCompressionParameters.from_level(
        LEVEL,
        window_log=_window_log(len(data) + len(base or b"")),
        enable_ldm=True,
        # Single-threaded: as small and as fast at 22 (measured), and one
        # window in memory instead of one per worker.
        threads=0,
    )
    dictionary = (
        zstd.ZstdCompressionDict(base, dict_type=zstd.DICT_TYPE_RAWCONTENT)
        if base is not None
        else None
    )
    return zstd.ZstdCompressor(
        compression_params=params, dict_data=dictionary
    ).compress(data)


def expand(blob: bytes, base: bytes | None = None) -> bytes:
    dictionary = (
        zstd.ZstdCompressionDict(base, dict_type=zstd.DICT_TYPE_RAWCONTENT)
        if base is not None
        else None
    )
    return zstd.ZstdDecompressor(
        dict_data=dictionary, max_window_size=2**MAX_WINDOW_LOG
    ).decompress(blob)


def _sha256(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def wheel_hashes(directory: str) -> dict[str, str]:
    return {
        os.path.basename(path): _sha256(path)
        for path in sorted(glob.glob(os.path.join(directory, "*.whl")))
    }


@dataclass
class Package:
    """An extracted offline package."""

    root: str
    version: str
    images: dict[str, SavedImages] = field(default_factory=dict)

    @classmethod
    def open(cls, path: str, scratch: str) -> Package:
        if os.path.isfile(path):
            log("delta", f"extracting {os.path.basename(path)}...")
            path = safe_extract(path, tempfile.mkdtemp(dir=scratch))
        root = descend(path)
        version_file = os.path.join(root, VERSION_FILE)
        if not os.path.isfile(version_file):
            raise Failure(f"{path}: no {VERSION_FILE} - not a release bundle")
        with open(version_file, encoding="utf-8") as handle:
            version = handle.read().strip()
        archives = sorted(glob.glob(os.path.join(root, "images", "*.tar")))
        if not archives:
            raise Failure(f"{path}: no images/*.tar - not an OFFLINE bundle")
        images = {os.path.basename(a): SavedImages(a) for a in archives}
        return cls(root, version, images)

    @property
    def diff_ids(self) -> set[str]:
        return set().union(*(saved.diff_ids for saved in self.images.values()))

    @property
    def tool(self) -> tuple[int, ...]:
        """The sb90-deploy version this bundle's hosts upgrade with."""
        try:
            with open(
                os.path.join(self.root, "requirements.txt"), encoding="utf-8"
            ) as f:
                for line in f:
                    if line.startswith("sb90-deploy=="):
                        return tuple(int(p) for p in line.split("==")[1].split("."))
        except (OSError, ValueError):
            pass
        return ()

    def counterpart(self, archive: str, image: int, index: int) -> str | None:
        saved = self.images.get(archive)
        if saved is None or image >= len(saved.images):
            return None
        layers = saved.images[image].diff_ids
        return layers[index] if index < len(layers) else None

    def layer(self, diff_id: str) -> bytes:
        for saved in self.images.values():
            if diff_id in saved.diff_ids:
                return saved.layer(diff_id)
        raise KeyError(diff_id)

    def close(self) -> None:
        for saved in self.images.values():
            saved.close()


def descend(root: str) -> str:
    """The tarball wraps the bundle in one directory; step into it."""
    if os.path.isfile(os.path.join(root, COMPOSE_FILE)):
        return root
    nested = [p for p in glob.glob(os.path.join(root, "*")) if os.path.isdir(p)]
    if len(nested) == 1 and os.path.isfile(os.path.join(nested[0], COMPOSE_FILE)):
        return nested[0]
    return root


def _hex(diff_id: str) -> str:
    return diff_id.split(":", 1)[1]


def plan_layer(
    diff_id: str,
    data: bytes,
    counterparts: dict[str, str | None],
    bases: dict[str, Package],
) -> tuple[dict, dict[str, bytes]]:
    """(delta.json entry, {blob name: bytes}) for one layer: one full blob, or
    one patch per distinct base counterpart - whichever is smaller.
    `counterparts` maps each base lacking the layer to its layer at the same
    position (None: it has none, so only a full blob serves it)."""
    full_name = _hex(diff_id) + ".zst"
    full = compress(data)
    if None in counterparts.values():
        return {"full": full_name}, {full_name: full}
    entry: dict[str, str] = {}
    blobs: dict[str, bytes] = {}
    for version, base_id in counterparts.items():
        if base_id in entry:
            continue
        name = f"{_hex(diff_id)}.{_hex(base_id)[:16]}.zst"
        blobs[name] = compress(data, bases[version].layer(base_id))
        entry[base_id] = name
        if sum(map(len, blobs.values())) >= len(full):
            return {"full": full_name}, {full_name: full}
    return {"patch": entry}, blobs


def craft(target_path: str, base_paths: list[str], out: str) -> str | None:
    """Write `<dir>-delta-<tag>.tar.zst` for `target_path` against every base
    whose hosts can apply it; None (and no file) when none can."""
    out_dir = os.path.abspath(out)
    os.makedirs(out_dir, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="sb90-delta-", dir=out_dir)
    packages: list[Package] = []
    try:
        target = Package.open(target_path, scratch)
        packages.append(target)
        spec = AppSpec.load(os.path.join(target.root, SPEC_FILE))
        bases: dict[str, Package] = {}
        for path in base_paths:
            base = Package.open(path, scratch)
            packages.append(base)
            if base.version == target.version:
                raise Failure(f"{path} is the target release {target.version}")
            if base.tool < FIRST_TOOL:
                warn(
                    f"skipping base {base.version}: bundled with an sb90-deploy"
                    " that cannot apply deltas - its hosts need the full package"
                )
                continue
            bases[base.version] = base
        if not base_paths:
            raise Failure("A delta needs at least one --base package.")
        if not bases:
            warn("no base can apply a delta - none written")
            return None
        stage = os.path.join(scratch, "stage", spec.bundle_dir)
        meta = _stage(target, bases, stage)
        archive = os.path.join(out_dir, spec.delta_archive(target.version))
        _pack(os.path.dirname(stage), spec.bundle_dir, archive)
        ok(
            f"packed {archive} ({os.path.getsize(archive) / 2**20:.1f} MiB,"
            f" bases: {', '.join(meta['bases'])})"
        )
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as handle:
                handle.write(f"delta={archive}\n")
        return archive
    finally:
        for package in packages:
            package.close()
        shutil.rmtree(scratch, ignore_errors=True)


def _stage(target: Package, bases: dict[str, Package], stage: str) -> dict:
    shutil.copytree(
        target.root, stage, ignore=shutil.ignore_patterns("images", "wheels")
    )
    blobs = os.path.join(stage, DELTA_DIR, "blobs")
    os.makedirs(blobs)
    base_ids = {version: base.diff_ids for version, base in bases.items()}
    images: dict[str, list[dict]] = {}
    layers: dict[str, dict] = {}
    for archive, saved in target.images.items():
        images[archive] = []
        for number, image in enumerate(saved.images):
            config = digest(image.config)
            with open(os.path.join(blobs, _hex(config) + ".json"), "wb") as handle:
                handle.write(image.config)
            images[archive].append(
                {"tags": image.tags, "config": config, "layers": image.diff_ids}
            )
            for index, diff_id in enumerate(image.diff_ids):
                missing = [v for v, ids in base_ids.items() if diff_id not in ids]
                if diff_id in layers or not missing:
                    continue
                log("delta", f"{archive} layer {index}: {diff_id[:19]}")
                counterparts = {
                    v: bases[v].counterpart(archive, number, index) for v in missing
                }
                layers[diff_id], written = plan_layer(
                    diff_id, saved.layer(diff_id), counterparts, bases
                )
                for name, blob in written.items():
                    with open(os.path.join(blobs, name), "wb") as handle:
                        handle.write(blob)
    wheels = wheel_hashes(os.path.join(target.root, "wheels"))
    base_wheels = [
        wheel_hashes(os.path.join(base.root, "wheels")) for base in bases.values()
    ]
    shipped = os.path.join(stage, DELTA_DIR, "wheels")
    os.makedirs(shipped)
    for name, sha in wheels.items():
        if any(known.get(name) != sha for known in base_wheels):
            shutil.copy2(os.path.join(target.root, "wheels", name), shipped)
    meta = {
        "format": FORMAT,
        "target": target.version,
        "bases": sorted(bases, reverse=True),
        "images": images,
        "layers": layers,
        "wheels": wheels,
    }
    with open(os.path.join(stage, DELTA_DIR, DELTA_FILE), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
    return meta


def _pack(parent: str, name: str, archive: str) -> None:
    # The blobs are compressed already; this only squeezes the text files.
    params = zstd.ZstdCompressionParameters.from_level(19, threads=-1)
    with (
        open(archive, "wb") as raw,
        zstd.ZstdCompressor(compression_params=params).stream_writer(raw) as stream,
        tarfile.open(fileobj=stream, mode="w|") as tar,
    ):
        tar.add(os.path.join(parent, name), arcname=name, filter=_modes)


# -- host side ----------------------------------------------------------------


def read_meta(root: str) -> dict | None:
    """delta.json when `root` is an extracted delta package, else None."""
    path = os.path.join(root, DELTA_DIR, DELTA_FILE)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        meta = json.load(handle)
    if meta.get("format", 0) > FORMAT:
        raise Failure(
            f"This delta package is format {meta.get('format')}; this tool reads"
            f" up to {FORMAT}.",
            "Upgrade with the full offline package instead.",
        )
    return meta


def materialize(root: str, meta: dict, base_archive: str, live_wheels: str) -> None:
    """Turn an extracted delta package into the full offline package it stands
    for: images/*.tar rebuilt from `base_archive` (a `docker save` of the
    images this host runs) + the shipped blobs, wheels/ from `live_wheels` +
    the shipped ones. Every layer and wheel is checked against its digest."""
    delta = os.path.join(root, DELTA_DIR)
    base = SavedImages(base_archive)
    local = base.diff_ids

    def blob(name: str) -> bytes:
        with open(os.path.join(delta, "blobs", name), "rb") as handle:
            return handle.read()

    def layer(diff_id: str) -> bytes:
        if diff_id in local:
            return base.layer(diff_id)
        entry = meta["layers"].get(diff_id)
        if entry is None:
            raise Failure(f"layer {diff_id[:19]} is neither shipped nor loaded here")
        if "full" in entry:
            data = expand(blob(entry["full"]))
        else:
            base_id = next((b for b in entry["patch"] if b in local), None)
            if base_id is None:
                raise Failure(f"no local layer to patch {diff_id[:19]} from")
            data = expand(blob(entry["patch"][base_id]), base.layer(base_id))
        if digest(data) != diff_id:
            raise Failure(f"rebuilt layer {diff_id[:19]} does not match its digest")
        return data

    try:
        images = os.path.join(root, "images")
        os.makedirs(images, exist_ok=True)
        for archive, entries in meta["images"].items():
            log("delta", f"rebuilding images/{archive}...")
            write_image(
                os.path.join(images, archive),
                [
                    Image(e["tags"], blob(_hex(e["config"]) + ".json"), e["layers"])
                    for e in entries
                ],
                layer,
            )
    finally:
        base.close()
    wheels = os.path.join(root, "wheels")
    os.makedirs(wheels, exist_ok=True)
    for name, sha in meta["wheels"].items():
        source = os.path.join(delta, "wheels", name)
        if not os.path.isfile(source):
            source = os.path.join(live_wheels, name)
        if not os.path.isfile(source) or _sha256(source) != sha:
            raise Failure(f"wheel {name} is neither shipped nor in {live_wheels}")
        shutil.copy2(source, wheels)
    shutil.rmtree(delta)
    ok(f"rebuilt the {meta['target']} offline package from the delta")
