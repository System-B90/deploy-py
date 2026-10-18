"""`python -m sb90_deploy <command>` — normally reached via a bundle's shims.

Run with no command on a terminal for an interactive menu.
"""

from __future__ import annotations

import subprocess
import sys
from enum import Enum
from types import SimpleNamespace
from typing import Annotated

import typer

from . import __version__, prompts
from .console import Failure, report

app = typer.Typer(
    name="sb90_deploy",
    help="Install, upgrade and release System-B90 app deployments.",
    no_args_is_help=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    add_completion=False,
    pretty_exceptions_enable=False,
)

DEPLOY = "Deployment"
RELEASE = "Release (CI / maintainer)"

Root = Annotated[str | None, typer.Option(help="Deployment directory (default: cwd).")]
AppJson = Annotated[
    str | None, typer.Option("--app", help="app.json (default: <root>/app.json).")
]


class Images(str, Enum):
    pull = "pull"
    local = "local"
    build = "build"
    skip = "skip"


class Bump(str, Enum):
    patch = "patch"
    minor = "minor"
    major = "major"


def _deployment(root, app_json):
    from .deployment import Deployment

    return Deployment.locate(root, app_json)


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version, is_eager=True, help="Show the version."
        ),
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if not prompts._interactive():
        typer.echo(ctx.get_help())
        raise typer.Exit(2)
    ctx.exit(_menu(ctx) or 0)


def _menu(ctx: typer.Context) -> int:
    """No command on a terminal: pick one from an arrow-key menu."""
    group = ctx.command
    choices = [
        (name, f"{name:<10} {group.commands[name].get_short_help_str(60)}")
        for name in group.list_commands(ctx)
    ]
    name = prompts.select(f"sb90_deploy {__version__} - what now?", choices, "install")
    if name in ("bundle", "delta", "wheels"):
        # Required options; not something to guess at from a menu.
        return _dispatch([name, "--help"])
    return _dispatch([name])


@app.command(rich_help_panel=DEPLOY)
def install(root: Root = None, app_json: AppJson = None) -> int:
    """First install from an extracted bundle."""
    from .install import install as run

    return run(_deployment(root, app_json), SimpleNamespace(root=root, app=app_json))


@app.command(rich_help_panel=DEPLOY)
def update(
    root: Root = None,
    app_json: AppJson = None,
    version: Annotated[
        str | None, typer.Option("--version", help="Target release tag.")
    ] = None,
    package: Annotated[
        str | None,
        typer.Option(
            help="Offline bundle (.tar.gz or extracted dir), or a delta (.tar.zst)."
        ),
    ] = None,
    pre_release: Annotated[
        bool, typer.Option("--pre-release", help="Include prereleases.")
    ] = False,
    skip_backup: Annotated[
        bool, typer.Option("--skip-backup", help="Accept no pre-upgrade dump.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask to proceed.")
    ] = False,
) -> int:
    """In-place upgrade (online, or --package <offline or delta bundle>)."""
    from .upgrade import upgrade

    args = SimpleNamespace(
        root=root,
        app=app_json,
        version=version,
        package=package,
        pre_release=pre_release,
        skip_backup=skip_backup,
        yes=yes,
    )
    return upgrade(_deployment(root, app_json), args)


@app.command("link-hive", rich_help_panel=DEPLOY)
def link_hive(root: Root = None, app_json: AppJson = None) -> int:
    """Co-locate with a Hive stack on the same Docker daemon."""
    from .linkhive import link_hive as run

    return run(_deployment(root, app_json), SimpleNamespace(root=root, app=app_json))


@app.command(rich_help_panel=DEPLOY)
def setup(root: Root = None, app_json: AppJson = None) -> int:
    """(Re-)run the app's setup.py .env wizard."""
    deployment = _deployment(root, app_json)
    return subprocess.call([sys.executable, "setup.py"], cwd=deployment.root)


@app.command(rich_help_panel=RELEASE)
def bundle(
    tag: Annotated[str, typer.Option(help="Release tag, e.g. v1.2.3.")],
    app_json: Annotated[str, typer.Option("--app")] = "deploy/app.json",
    out: str = "dist",
    repo: Annotated[
        str | None,
        typer.Option(help="Repo root the bundle.files paths are relative to."),
    ] = None,
    images: Annotated[
        Images,
        typer.Option(
            help="Offline images: [b]pull[/b] from the registry, use [b]local[/b]"
            " ones as-is, [b]build[/b] the app's own from source (others pulled),"
            " or [b]skip[/b]."
        ),
    ] = Images.pull,
    build_compose: Annotated[
        str | None,
        typer.Option(
            help="With --images build: compose file with build: contexts,"
            " relative to --repo (default: bundle.build_compose in app.json,"
            " else deploy/docker-compose.yml)."
        ),
    ] = None,
    no_wheels: Annotated[bool, typer.Option("--no-wheels")] = False,
) -> int:
    """Craft the online + offline release archives."""
    from .bundle import craft

    craft(app_json, tag, out, repo, images.value, not no_wheels, build_compose)
    return 0


@app.command(rich_help_panel=RELEASE)
def delta(
    target: Annotated[
        str, typer.Option(help="The new release's offline bundle (.tar.gz or dir).")
    ],
    bases: Annotated[
        list[str],
        typer.Option(
            "--base",
            help="Offline bundle of a release the delta upgrades from. Repeatable:"
            " one delta then serves every base.",
        ),
    ],
    out: str = "dist",
) -> int:
    """Craft <dir>-delta-<tag>.tar.zst: only what the bases' hosts lack."""
    from .delta import craft

    craft(target, bases, out)
    return 0


@app.command(rich_help_panel=RELEASE)
def wheels(
    requirements: Annotated[
        list[str], typer.Option("--requirement", "-r", help="Repeatable.")
    ],
    out: str = "wheels",
    project: Annotated[
        str | None, typer.Option(help="Also build this project's own wheel.")
    ] = None,
    platforms: Annotated[
        list[str] | None, typer.Option("--platform", help="Repeatable.")
    ] = None,
    pythons: Annotated[
        list[str] | None, typer.Option("--python", help="Repeatable.")
    ] = None,
) -> int:
    """Air-gapped wheels for a pip-installable tool (no app.json)."""
    from .bundle import DEFAULT_PLATFORMS, DEFAULT_PYTHONS, wheels_for_tool

    wheels_for_tool(
        requirements,
        out,
        platforms or DEFAULT_PLATFORMS,
        pythons or DEFAULT_PYTHONS,
        project,
    )
    return 0


@app.command(rich_help_panel=RELEASE)
def publish(
    app_json: Annotated[
        str, typer.Option("--app", help="app.json, relative to --repo.")
    ] = "deploy/app.json",
    repo: Annotated[str, typer.Option(help="Repo root (default: cwd).")] = ".",
    bump: Annotated[Bump | None, typer.Option(help="Skip the bump prompt.")] = None,
    rc: Annotated[
        bool | None, typer.Option("--rc/--no-rc", help="Release candidate.")
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Explicit version instead of the derived one."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Allow --version <= latest tag.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation.")
    ] = False,
    dry: Annotated[
        bool, typer.Option("--dry", help="Commit and tag locally, push nothing.")
    ] = False,
) -> int:
    """Bump manifests, tag and push the next release."""
    from .release import publish as run

    return run(
        SimpleNamespace(
            app=app_json,
            repo=repo,
            bump=bump.value if bump else None,
            rc=rc,
            version=version,
            force=force,
            yes=yes,
            dry=dry,
        )
    )


def _dispatch(argv: list[str]) -> int:
    command = typer.main.get_command(app)
    try:
        result = command.main(argv, prog_name="sb90_deploy", standalone_mode=False)
    except typer.Exit as done:
        return done.exit_code
    except typer.TyperException as error:
        error.show()
        return error.exit_code
    except typer.Abort:
        return 130
    except Failure as failure:
        report(failure)
        return 1
    except subprocess.CalledProcessError as error:
        report(
            Failure(
                f"Command failed ({error.returncode}): {' '.join(map(str, error.cmd))}",
                (error.stderr or "").strip(),
            )
        )
        return 1
    except KeyboardInterrupt:
        return 130
    return result if isinstance(result, int) else 0


def main(argv: list[str] | None = None) -> int:
    return _dispatch(list(sys.argv[1:] if argv is None else argv))
