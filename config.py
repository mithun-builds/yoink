"""
Persistent settings for Yoink, shared by the web UI and the CLI scripts.

Stored as JSON next to the code so a single local install has one place to
change the download folder. Precedence, highest first:

    1. an explicit --out flag / "out" field in an API request
    2. the YOINK_OUT environment variable
    3. out_dir in yoink.config.json (what the UI's "Save as default" writes)
    4. "downloads"
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "yoink.config.json"
FALLBACK_OUT = "downloads"


def load_config() -> dict:
    data = {}
    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}  # unreadable or hand-edited into invalid JSON; use defaults
    if not isinstance(data, dict):
        data = {}
    data.setdefault("out_dir", os.environ.get("YOINK_OUT") or FALLBACK_OUT)
    return data


def save_config(**changes) -> dict:
    data = load_config()
    data.update({k: v for k, v in changes.items() if v is not None})
    CONFIG_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data


def default_out() -> str:
    """The configured download folder, as written (may be relative)."""
    return os.environ.get("YOINK_OUT") or load_config()["out_dir"]


def resolve_out(out=None) -> Path:
    """Absolute path for a download folder.

    Relative paths resolve against the code directory rather than the shell's
    cwd, so the UI and the CLI agree on where "downloads" is regardless of
    where they were started from.
    """
    raw = out or default_out()
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


#  code directories that live alongside the downloads folder and would only
#  be noise in the UI's folder picker
SKIP_DIRS = {"venv", ".venv", "__pycache__", "templates", "static", ".git", "node_modules"}


def list_subfolders(parent=None):
    """Immediate subfolders of the configured folder's parent, to offer as
    quick switches in the UI -- one folder per playlist is a common habit."""
    base = resolve_out(parent)
    candidates = []
    for d in (base, base.parent):
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir() and not p.name.startswith(".") and p.name not in SKIP_DIRS:
                candidates.append(str(p))
    seen, out = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out
