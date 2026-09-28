"""Interactive prompts: InquirerPy on a terminal, plain input() otherwise.

InquirerPy is the org's prompt library (Bluz's CLI and its old wizard used
it). It needs a real terminal, though, and ./install.sh is sometimes run with
stdin piped (automation, `yes |`, CI smoke tests) — there it would fail
outright, so those runs get plain prompts with the same defaults.
"""

from __future__ import annotations

import getpass
import sys


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def text(message: str, default: str = "") -> str:
    if _interactive():
        from InquirerPy import inquirer

        return (inquirer.text(message=message, default=default).execute() or "").strip()
    suffix = f" [{default}]" if default else ""
    try:
        return input(f"{message}{suffix}: ").strip() or default
    except EOFError:
        return default


def select(message: str, choices: list[tuple[str, str]], default: str) -> str:
    """One of `choices` (value, label). Arrow-key menu on a terminal; otherwise
    a typed value, falling back to `default` on empty input or EOF."""
    if _interactive():
        from InquirerPy import inquirer
        from InquirerPy.base.control import Choice

        return inquirer.select(
            message=message,
            choices=[Choice(value, name=label) for value, label in choices],
            default=default,
        ).execute()
    values = "/".join(value for value, _ in choices)
    try:
        answer = input(f"{message} ({values}) [{default}]: ").strip().lower()
    except EOFError:
        return default
    return answer or default


def secret(message: str) -> str:
    if _interactive():
        from InquirerPy import inquirer

        return inquirer.secret(message=message).execute() or ""
    return getpass.getpass(f"{message}: ")


def confirm(message: str, default: bool = False) -> bool:
    if _interactive():
        from InquirerPy import inquirer

        return bool(inquirer.confirm(message=message, default=default).execute())
    try:
        answer = input(message + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    except EOFError:
        return default
    return default if not answer else answer.startswith("y")
