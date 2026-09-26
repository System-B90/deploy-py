"""Reading and editing a deployment's `.env` without python-dotenv.

Two consumers read this file and they disagree on details, so the writer stays
inside what both accept:

* Docker Compose, for `${VAR}` substitution in the compose file and for
  `env_file:`. It interpolates `$` inside unquoted and double-quoted values.
* The apps themselves (Next.js / dotenv), when the file is mounted in.

So a value is written bare when it is plainly safe, single-quoted (literal in
both) otherwise, and double-quoted only when it itself contains a single quote.

Edits are line-preserving: `set_value` rewrites one `KEY=` line in place and
leaves comments, ordering and every key it does not own alone. The installers
own `<APP>_VERSION` and `HIVE_NETWORK_NAME`; the wizard owns the rest; neither
may drop the other's keys (peek-a-boo lost `HIVE_NETWORK_NAME` to exactly that).
"""

import os
import re
from collections import OrderedDict

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")
_SAFE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]*$")


def _unquote(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        inner = raw[1:-1]
        if raw[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    # An unquoted value ends at an inline comment, which needs leading space.
    hash_at = raw.find(" #")
    if hash_at >= 0:
        raw = raw[:hash_at].rstrip()
    return raw


def parse(text):
    values = OrderedDict()
    for line in text.splitlines():
        match = _LINE.match(line)
        if match and not line.lstrip().startswith("#"):
            values[match.group(1)] = _unquote(match.group(2))
    return values


def read(path):
    """Every key in the file, in order; an absent file is an empty mapping."""
    if not os.path.isfile(path):
        return OrderedDict()
    with open(path, encoding="utf-8") as handle:
        return parse(handle.read())


def get(path, key, default=""):
    value = read(path).get(key)
    return default if value in (None, "") else value


def format_value(value):
    value = "" if value is None else str(value)
    if _SAFE.match(value):
        return value
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write(path, values):
    """Replace the whole file with `values` (an ordered mapping)."""
    body = "".join(f"{k}={format_value(v)}\n" for k, v in values.items())
    _atomic_write(path, body)


def set_value(path, key, value):
    """Set one key, editing its line in place or appending it."""
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    rendered = f"{key}={format_value(value)}"
    replaced = False
    for index, line in enumerate(lines):
        match = _LINE.match(line)
        if match and match.group(1) == key and not line.lstrip().startswith("#"):
            lines[index] = rendered
            replaced = True
    if not replaced:
        lines.append(rendered)
    _atomic_write(path, "\n".join(lines) + "\n")


def has_key(path, key):
    return key in read(path)
