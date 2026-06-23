"""
Local teammate aliases.

GitHub handles are awkward to type and remember; colleagues know each other
by name. An alias is a short local name (set once on your laptop) that maps
to a teammate's GitHub handle, so `/ask om ...` resolves to `om-divyatej`.

Aliases are local to each machine — they're a personal convenience, never
shared. Stored next to the auth file at ~/.teammate-sync/aliases.json.
"""

import json
import os
from pathlib import Path

DEFAULT_ALIASES_FILE = "~/.teammate-sync/aliases.json"


def aliases_file_path() -> Path:
    return Path(os.environ.get("TEAMMATE_ALIASES_FILE", DEFAULT_ALIASES_FILE)).expanduser()


def read_aliases() -> dict[str, str]:
    """Return {alias: handle}. Empty dict if no aliases set yet."""
    path = aliases_file_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _write_aliases(aliases: dict[str, str]) -> None:
    path = aliases_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(aliases, indent=2) + "\n")


def set_alias(name: str, handle: str) -> None:
    aliases = read_aliases()
    aliases[name.strip().lower()] = handle.strip()
    _write_aliases(aliases)


def remove_alias(name: str) -> bool:
    """Delete an alias. Returns True if it existed."""
    aliases = read_aliases()
    key = name.strip().lower()
    if key not in aliases:
        return False
    del aliases[key]
    _write_aliases(aliases)
    return True


def resolve(name: str) -> str:
    """Map a typed name to a handle via aliases; unknown names pass through
    unchanged so real handles still work."""
    return read_aliases().get(name.strip().lower(), name)
