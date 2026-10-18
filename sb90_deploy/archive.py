"""Reading release archives: safe extraction and `docker save` image tars.

A `docker save` tar differs by daemon - legacy `<id>/layer.tar`, OCI
`blobs/sha256/<digest>`, layers uncompressed or (containerd image store) gzip /
zstd as pulled. `SavedImages` hides that: layers are addressed by their diffID
(sha256 of the uncompressed tar, from the image config), which is the same
everywhere, and every read is verified against it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass

import zstandard

from .console import Failure

# Members are vetted by _check_members; "data" also drops odd modes.
_FILTER = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
_GZIP = b"\x1f\x8b"
_ZSTD = b"\x28\xb5\x2f\xfd"


def _check_members(members, archive: str, root: str):
    root = os.path.realpath(root)
    for member in members:
        dest = os.path.realpath(os.path.join(root, member.name))
        if not dest.startswith(root + os.sep) and dest != root:
            raise Failure(f"{archive} contains an unsafe path: {member.name}")
        yield member


def safe_extract(archive: str, target: str) -> str:
    """Extract `archive` (.tar, .tar.gz, .tar.zst) into `target`, refusing
    anything that would land outside it."""
    if archive.endswith(".zst"):
        with (
            open(archive, "rb") as raw,
            zstandard.ZstdDecompressor().stream_reader(raw) as stream,
            tarfile.open(fileobj=stream, mode="r|") as tar,
        ):
            for member in _check_members(tar, archive, target):
                tar.extract(member, target, **_FILTER)
        return target
    with tarfile.open(archive) as tar:
        tar.extractall(
            target, members=list(_check_members(tar, archive, target)), **_FILTER
        )
    return target


def decompress(data: bytes) -> bytes:
    if data[:2] == _GZIP:
        return gzip.decompress(data)
    if data[:4] == _ZSTD:
        return zstandard.ZstdDecompressor().stream_reader(data).read()
    return data


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Image:
    tags: list[str]
    config: bytes
    diff_ids: list[str]


class SavedImages:
    """One `docker save` archive, read lazily by layer diffID."""

    def __init__(self, path: str):
        self.path = path
        self.tar = tarfile.open(path)  # noqa: SIM115 - closed by close()
        try:
            manifest = json.load(self.tar.extractfile("manifest.json"))
        except (KeyError, ValueError, tarfile.TarError) as error:
            raise Failure(f"{path} is not a docker save archive: {error}") from error
        self.images: list[Image] = []
        self._members: dict[str, str] = {}
        for entry in manifest:
            config = self.tar.extractfile(entry["Config"]).read()
            diff_ids = json.loads(config)["rootfs"]["diff_ids"]
            self.images.append(Image(entry.get("RepoTags") or [], config, diff_ids))
            for diff_id, member in zip(diff_ids, entry["Layers"], strict=True):
                self._members.setdefault(diff_id, member)

    @property
    def diff_ids(self) -> set[str]:
        return set(self._members)

    def layer(self, diff_id: str) -> bytes:
        """The uncompressed layer tar, byte-identical on every daemon."""
        data = decompress(self.tar.extractfile(self._members[diff_id]).read())
        if digest(data) != diff_id:
            raise Failure(
                f"{os.path.basename(self.path)}: layer {diff_id[:19]} does not"
                " match its digest"
            )
        return data

    def close(self) -> None:
        self.tar.close()


def write_image(path: str, images: list[Image], layer) -> None:
    """A `docker load`-able archive: manifest.json, configs, uncompressed layers.
    `layer(diff_id)` returns each layer's bytes; one is held at a time."""

    def add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))

    manifest = []
    written: set[str] = set()
    with tarfile.open(path, "w") as tar:
        for image in images:
            config = "blobs/sha256/" + digest(image.config).split(":", 1)[1]
            names = ["blobs/sha256/" + d.split(":", 1)[1] for d in image.diff_ids]
            if config not in written:
                add(tar, config, image.config)
                written.add(config)
            for diff_id, name in zip(image.diff_ids, names, strict=True):
                if name not in written:
                    add(tar, name, layer(diff_id))
                    written.add(name)
            manifest.append({"Config": config, "RepoTags": image.tags, "Layers": names})
        add(tar, "manifest.json", json.dumps(manifest).encode())
