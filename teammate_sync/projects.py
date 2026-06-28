"""
Project identity — the canonical name that ties sessions across machines.

A folder's git-repo root is mapped (stickily) to a canonical project name the
user picked via `set-project`. Sam's ~/work/gmr and Dario's ~/Code/gmr-qc/codebase
both map to the same canonical "gmr-qc", so /ask targeting, per-project
knowledge, and cross-engineer linking all join on one human-chosen key instead
of a per-machine folder guess.

The map is LOCAL only (~/.teammate-sync/project-map.json) — nothing about your
paths leaves the machine. The canonical NAME registry is shared org-wide (backend
/v1/projects) so the picker can show what teammates are working on.
"""
import json
import subprocess
from pathlib import Path

_MAP = "~/.teammate-sync/project-map.json"


def _map_path() -> Path:
    return Path(_MAP).expanduser()


def _read_map() -> dict:
    try:
        return json.loads(_map_path().read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def repo_root(cwd: str) -> str:
    """The git toplevel of cwd (so deep paths like .../gmr-qc/codebase resolve to
    the repo), else cwd itself. This is the sticky-map key."""
    if not cwd:
        return cwd
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path(cwd))


def git_basename(cwd: str) -> str:
    """Fallback label when no canonical project is set: the repo/dir name."""
    root = repo_root(cwd)
    return Path(root).name if root else "session"


def project_for_cwd(cwd: str) -> str | None:
    """Canonical project for a folder via the sticky map, or None if never set."""
    if not cwd:
        return None
    return _read_map().get(repo_root(cwd))


def label_for_cwd(cwd: str) -> str:
    """What to SHOW for a session: canonical project if set, else the repo name."""
    return project_for_cwd(cwd) or git_basename(cwd)


def set_project_for_cwd(cwd: str, name: str) -> str:
    """Sticky-map this folder's repo root → canonical project `name`. Returns the
    repo root that was mapped."""
    root = repo_root(cwd)
    d = _read_map()
    d[root] = name
    p = _map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))
    return root
