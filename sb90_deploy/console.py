"""Operator-facing output: the `[OK]` / `[WAIT]` / `[ERROR]` vocabulary of the old
bash installers, kept so docs and muscle memory still match."""

import os
import sys

_COLORS = {
    "red": "\033[1;31m",
    "green": "\033[1;32m",
    "yellow": "\033[1;33m",
    "blue": "\033[1;34m",
    "cyan": "\033[1;36m",
}
_RESET = "\033[0m"

# Windows consoles and pipes default to a legacy code page; an app wizard's
# emoji or an em-dash would otherwise raise UnicodeEncodeError mid-install.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass


def _use_color():
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Windows 10+ consoles understand ANSI once VT processing is on; asking
        # for it is harmless where it is already enabled.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:  # noqa: BLE001 - colour is cosmetic
            return False
    return True


COLOR = _use_color()


def paint(color, text):
    if not COLOR:
        return text
    return _COLORS[color] + text + _RESET


def say(text=""):
    print(text)
    sys.stdout.flush()


def banner(text, color="cyan"):
    line = "=" * max(41, len(text) + 8)
    say(paint(color, line))
    say(paint(color, text.center(len(line))))
    say(paint(color, line))


def ok(text):
    say(paint("green", "[OK]") + " " + text)


def wait(text):
    say("\n" + paint("yellow", "[WAIT] " + text))


def info(text):
    say(paint("blue", ">> ") + text)


def log(tag, text):
    say(paint("cyan", "[" + tag + "]") + " " + text)


def warn(text, *hints):
    say("\n" + paint("yellow", "[WARN] " + text))
    for hint in hints:
        say("        " + hint)


class Failure(Exception):
    """A step failed in a way the operator can act on.

    `hints` are the follow-up lines printed under the headline — the actual
    fix, not a restatement of the error.
    """

    def __init__(self, message, *hints):
        Exception.__init__(self, message)
        self.message = message
        self.hints = list(hints)


def report(failure):
    say("\n" + paint("red", "[ERROR] " + failure.message))
    for hint in failure.hints:
        say("        " + hint)


def confirm(question, default=False, assume_yes=False):
    if assume_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        reply = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not reply:
        return default
    return reply.startswith("y")
