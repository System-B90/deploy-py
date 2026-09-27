"""`python -m sb90_deploy <command>` — normally reached via a bundle's shims.

install      first install from an extracted bundle
update       in-place upgrade (online, or --package <offline bundle>)
link-hive    co-locate with a Hive stack on the same Docker daemon
setup        (re-)run the app's setup.py wizard
bundle       CI: craft the online + offline release archives
publish      maintainer: bump manifests, tag and push the next release
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from . import __version__
from .console import Failure, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sb90_deploy",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def deployment_args(p):
        p.add_argument("--root", help="deployment directory (default: cwd)")
        p.add_argument("--app", help="path to app.json (default: <root>/app.json)")

    deployment_args(sub.add_parser("install", help="first install from a bundle"))
    deployment_args(sub.add_parser("setup", help="run the .env wizard"))
    deployment_args(sub.add_parser("link-hive", help="link to a co-located Hive"))

    update = sub.add_parser("update", help="in-place upgrade")
    deployment_args(update)
    update.add_argument("--version", dest="version", help="target release tag")
    update.add_argument("--package", help="offline bundle (.tar.gz or extracted dir)")
    update.add_argument(
        "--pre-release", action="store_true", help="include prereleases"
    )
    update.add_argument(
        "--skip-backup", action="store_true", help="accept no pre-upgrade dump"
    )
    update.add_argument(
        "--yes", "-y", action="store_true", help="do not ask to proceed"
    )

    bundle = sub.add_parser("bundle", help="CI: craft release archives")
    bundle.add_argument("--app", default="deploy/app.json")
    bundle.add_argument("--tag", required=True)
    bundle.add_argument("--out", default="dist")
    bundle.add_argument(
        "--repo", help="repo root the bundle.files paths are relative to"
    )
    bundle.add_argument("--images", choices=["pull", "local", "skip"], default="pull")
    bundle.add_argument("--no-wheels", action="store_true")

    from .release import add_arguments

    add_arguments(sub.add_parser("publish", help="tag and push the next release"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "bundle":
            from .bundle import craft

            craft(
                args.app, args.tag, args.out, args.repo, args.images, not args.no_wheels
            )
            return 0

        if args.command == "publish":
            from .release import publish

            return publish(args)

        from .deployment import Deployment

        deployment = Deployment.locate(args.root, args.app)
        if args.command == "install":
            from .install import install

            return install(deployment, args)
        if args.command == "update":
            from .upgrade import upgrade

            return upgrade(deployment, args)
        if args.command == "link-hive":
            from .linkhive import link_hive

            return link_hive(deployment, args)
        if args.command == "setup":
            return subprocess.call([sys.executable, "setup.py"], cwd=deployment.root)
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
    return 0
