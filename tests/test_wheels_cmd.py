"""`sb90_deploy wheels`: standalone wheel vendoring for non-Docker tools (#9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sb90_deploy import bundle, cli
from sb90_deploy.console import Failure


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {"download": [], "run": []}

    def fake_download(requirements_file, wheels, platforms, pythons, index):
        seen["download"].append(
            (
                requirements_file.read_text(encoding="utf-8"),
                wheels,
                platforms,
                pythons,
                index,
            )
        )
        (wheels / "requests-2.0-py3-none-any.whl").write_text("")

    monkeypatch.setattr(bundle, "download_wheels", fake_download)
    monkeypatch.setattr(
        bundle.subprocess, "run", lambda argv, **kw: seen["run"].append(argv)
    )
    return seen


def test_combines_requirement_files_for_each_target(
    tmp_path: Path, calls: dict
) -> None:
    (tmp_path / "a.txt").write_text("requests\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("typer\n", encoding="utf-8")
    out = tmp_path / "wheels"

    count = bundle.wheels_for_tool(
        [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],
        str(out),
        ["win_amd64"],
        ["3.12"],
    )

    ((reqs, wheels, platforms, pythons, index),) = calls["download"]
    assert "requests" in reqs and "typer" in reqs
    assert (wheels, platforms, pythons) == (out, ["win_amd64"], ["3.12"])
    assert index == bundle.DEFAULT_PIP_INDEX
    assert count == 1
    assert not (out / ".requirements.txt").exists()  # scratch file cleaned up


def test_builds_the_project_wheel_when_asked(tmp_path: Path, calls: dict) -> None:
    (tmp_path / "r.txt").write_text("requests\n", encoding="utf-8")
    bundle.wheels_for_tool(
        [str(tmp_path / "r.txt")],
        str(tmp_path / "w"),
        ["win_amd64"],
        ["3.12"],
        project=".",
    )
    (argv,) = calls["run"]
    assert argv[-5:] == ["wheel", "--no-deps", "-w", str(tmp_path / "w"), "."]


def test_missing_requirements_file_fails(tmp_path: Path, calls: dict) -> None:
    with pytest.raises(Failure, match="requirements file not found"):
        bundle.wheels_for_tool(
            [str(tmp_path / "nope.txt")], str(tmp_path / "w"), ["win_amd64"], ["3.12"]
        )


def test_cli_defaults_to_every_platform_and_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list = []
    monkeypatch.setattr(bundle, "wheels_for_tool", lambda *a: seen.append(a))
    assert cli.main(["wheels", "-r", "requirements.txt", "--out", str(tmp_path)]) == 0
    ((requirements, out, platforms, pythons, project),) = seen
    assert out == str(tmp_path)
    assert requirements == ["requirements.txt"]
    assert platforms == bundle.DEFAULT_PLATFORMS
    assert pythons == bundle.DEFAULT_PYTHONS
    assert project is None
