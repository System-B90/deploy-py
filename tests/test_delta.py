"""Delta packages: craft against several bases, rebuild on a host, upgrade with it."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from conftest import make_bundle, run_cli

from sb90_deploy import delta
from sb90_deploy.archive import Image, SavedImages, digest, safe_extract, write_image
from sb90_deploy.console import Failure

OS_LAYER = random.Random(0).randbytes(150_000)
NGINX = [random.Random(1).randbytes(60_000)]


def _app_layer(version: str) -> bytes:
    """Mostly the same bytes in every release, like a real app layer."""
    data = bytearray(random.Random(2).randbytes(200_000))
    data[1000:1010] = version.encode().ljust(10, b"_")
    return bytes(data)


def _image(tag: str, layers: list[bytes]) -> tuple[Image, dict[str, bytes]]:
    by_id = {digest(layer): layer for layer in layers}
    config = json.dumps(
        {"rootfs": {"type": "layers", "diff_ids": list(by_id)}, "tag": tag}
    ).encode()
    return Image([tag], config, list(by_id)), by_id


def _save(path: Path, images: list[tuple[Image, dict[str, bytes]]]) -> Path:
    layers = {k: v for _, by_id in images for k, v in by_id.items()}
    write_image(str(path), [image for image, _ in images], layers.__getitem__)
    return path


def _ui(version: str, extra: bytes = b""):
    layers = [OS_LAYER, _app_layer(version)] + ([extra] if extra else [])
    return _image(f"ghcr.io/system-b90/demo/ui:{version}", layers)


def _nginx():
    return _image("nginx:1.27-alpine", NGINX)


def _offline(
    root: Path, version: str, wheels: dict[str, bytes], ui=None, tool="0.6.0"
) -> Path:
    make_bundle(root, version, extra={"requirements.txt": f"sb90-deploy=={tool}\n"})
    (root / "images").mkdir()
    _save(root / "images" / "demo-ui.tar", [ui or _ui(version)])
    _save(root / "images" / "nginx.tar", [_nginx()])
    (root / "wheels").mkdir()
    for name, data in wheels.items():
        (root / "wheels" / name).write_bytes(data)
    return root


WHEELS_1 = {"sb90_deploy-1.whl": b"deploy-1", "dep-1.whl": b"dep"}
WHEELS_2 = {"sb90_deploy-2.whl": b"deploy-2", "dep-1.whl": b"dep"}


@pytest.fixture
def packages(tmp_path):
    """v1.0.0 and v1.1.0 bases (v1.1.0 adds a layer), v2.0.0 target."""
    return {
        "v1.0.0": _offline(tmp_path / "v100" / "demo", "v1.0.0", WHEELS_1),
        "v1.1.0": _offline(
            tmp_path / "v110" / "demo",
            "v1.1.0",
            WHEELS_1,
            _ui("v1.1.0", b"extra layer"),
        ),
        "v2.0.0": _offline(tmp_path / "v200" / "demo", "v2.0.0", WHEELS_2),
    }


def _craft(packages, tmp_path, *bases):
    return delta.craft(
        str(packages["v2.0.0"]),
        [str(packages[b]) for b in bases],
        str(tmp_path / "out"),
    )


def _extract(archive: str, tmp_path: Path) -> Path:
    return Path(safe_extract(archive, str(tmp_path / "x"))) / "demo"


def test_ships_only_patches_and_new_wheels(packages, tmp_path):
    archive = _craft(packages, tmp_path, "v1.0.0", "v1.1.0")
    root = _extract(archive, tmp_path)
    meta = delta.read_meta(str(root))

    assert archive.endswith("demo-delta-v2.0.0.tar.zst")
    assert meta["target"] == "v2.0.0" and meta["bases"] == ["v1.1.0", "v1.0.0"]
    # OS layer and nginx are on every base: not shipped. The app layer is
    # patched once per distinct base counterpart - here two.
    assert list(meta["layers"]) == [digest(_app_layer("v2.0.0"))]
    entry = meta["layers"][digest(_app_layer("v2.0.0"))]
    assert set(entry["patch"]) == {
        digest(_app_layer("v1.0.0")),
        digest(_app_layer("v1.1.0")),
    }
    shipped = sum(p.stat().st_size for p in (root / ".delta" / "blobs").glob("*.zst"))
    assert shipped < 2_000  # vs 200 KB of incompressible layer
    assert sorted(p.name for p in (root / ".delta" / "wheels").iterdir()) == [
        "sb90_deploy-2.whl"
    ]
    assert not (root / "images").exists() and (root / "docker-compose.yml").exists()


@pytest.mark.parametrize("base", ["v1.0.0", "v1.1.0"])
def test_any_base_rebuilds_the_exact_target(packages, tmp_path, base):
    root = _extract(_craft(packages, tmp_path, "v1.0.0", "v1.1.0"), tmp_path)
    # What the host's `docker save` of its running images gives.
    running = tmp_path / "base.tar"
    saved = [SavedImages(str(p)) for p in (packages[base] / "images").glob("*.tar")]
    layers = {d: s.layer(d) for s in saved for d in s.diff_ids}
    _save(running, [(i, layers) for s in saved for i in s.images])
    for s in saved:
        s.close()

    delta.materialize(
        str(root),
        delta.read_meta(str(root)),
        str(running),
        str(packages[base] / "wheels"),
    )

    for archive in ("demo-ui.tar", "nginx.tar"):
        rebuilt = SavedImages(str(root / "images" / archive))
        expected = SavedImages(str(packages["v2.0.0"] / "images" / archive))
        assert [i.config for i in rebuilt.images] == [i.config for i in expected.images]
        assert all(rebuilt.layer(d) == expected.layer(d) for d in expected.diff_ids)
        rebuilt.close()
        expected.close()
    assert {p.name: p.read_bytes() for p in (root / "wheels").iterdir()} == WHEELS_2
    assert not (root / ".delta").exists()


def test_a_full_blob_when_no_base_has_a_counterpart(packages, tmp_path):
    packages["v2.0.0"] = _offline(
        tmp_path / "v201" / "demo", "v2.0.0", WHEELS_2, _ui("v2.0.0", b"brand new")
    )
    root = _extract(_craft(packages, tmp_path, "v1.0.0"), tmp_path)
    assert {"full": digest(b"brand new")[7:] + ".zst"} in delta.read_meta(str(root))[
        "layers"
    ].values()


def test_a_host_without_the_base_images_is_refused(packages, tmp_path):
    root = _extract(_craft(packages, tmp_path, "v1.0.0"), tmp_path)
    wrong = tmp_path / "wrong.tar"
    _save(wrong, [_nginx()])  # pruned: no OS layer, no app layer to patch
    with pytest.raises(Failure, match="neither shipped nor loaded here"):
        delta.materialize(
            str(root), delta.read_meta(str(root)), str(wrong), str(tmp_path)
        )


def test_bases_whose_tools_predate_deltas_are_skipped(packages, tmp_path, capsys):
    _offline(tmp_path / "old" / "demo", "v0.9.0", WHEELS_1, tool="0.1.0")
    packages["v0.9.0"] = tmp_path / "old" / "demo"

    root = _extract(_craft(packages, tmp_path, "v0.9.0", "v1.0.0"), tmp_path)

    assert delta.read_meta(str(root))["bases"] == ["v1.0.0"]
    assert "skipping base v0.9.0" in capsys.readouterr().out
    assert _craft(packages, tmp_path / "none", "v0.9.0") is None


# -- through `update --package` ------------------------------------------------


def _live(packages, tmp_path, version="v1.0.0", monkeypatch=None):
    root = packages[version]
    (root / ".env").write_text(f"DEMO_VERSION={version}\nSECRET=keep-me\n")
    (root / "nginx" / "ssl").mkdir(parents=True)
    (root / "nginx" / "ssl" / "cert.pem").write_text("old-cert\n")
    saved = [SavedImages(str(p)) for p in (root / "images").glob("*.tar")]
    layers = {d: s.layer(d) for s in saved for d in s.diff_ids}
    running = _save(
        tmp_path / "running.tar", [(i, layers) for s in saved for i in s.images]
    )
    for s in saved:
        s.close()
    monkeypatch.setenv("STUB_SAVE_FROM", str(running))
    return root


def test_update_with_a_delta_rebuilds_loads_and_rolls(
    packages, tmp_path, calls, monkeypatch
):
    archive = _craft(packages, tmp_path, "v1.0.0")
    live = _live(packages, tmp_path, monkeypatch=monkeypatch)
    monkeypatch.setenv("STUB_IMAGE", "ghcr.io/system-b90/demo/ui:v2.0.0")

    assert run_cli("update", "--root", str(live), "--yes", "--package", archive) == 0

    joined = calls.joined()
    assert any(c.startswith("save -o ") and "demo/ui:v1.0.0" in c for c in joined)
    loads = [c for c in joined if c.startswith("load -i ")]
    assert [Path(c.split()[-1]).name for c in loads] == ["demo-ui.tar", "nginx.tar"]
    assert (live / "VERSION").read_text().strip() == "v2.0.0"
    assert "DEMO_VERSION=v2.0.0" in (live / ".env").read_text()
    assert sorted(p.name for p in (live / "wheels").iterdir()) == sorted(WHEELS_2)
    assert not (live / ".delta").exists()


def test_update_refuses_a_delta_for_another_base(
    packages, tmp_path, calls, monkeypatch, capsys
):
    archive = _craft(packages, tmp_path, "v1.1.0")
    live = _live(packages, tmp_path, monkeypatch=monkeypatch)

    assert run_cli("update", "--root", str(live), "--yes", "--package", archive) == 1

    out = capsys.readouterr().out
    assert "upgrades v1.1.0 to v2.0.0" in out and "demo-offline-v2.0.0.tar.gz" in out
    assert not any(c.startswith("save") for c in calls.joined())
    assert (live / "VERSION").read_text().strip() == "v1.0.0"
