"""Run configuration. Secrets come from .env, everything else from config.yaml.

The API key is read from the file straight into the process environment and is never logged,
printed, echoed or returned. Nothing in this harness renders it.
"""
from __future__ import annotations

import os
import pathlib
from typing import Any, Dict

_ROOT = pathlib.Path(__file__).resolve().parent
_ENV = _ROOT / ".env"
SECRET_KEYS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def load_env(path: pathlib.Path = _ENV) -> list:
    """Read .env into os.environ. Returns the NAMES that were set, never the values."""
    if not path.exists():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key, value)
            loaded.append(key)
    return loaded


def load(path: pathlib.Path = _ROOT / "config.yaml") -> Dict[str, Any]:
    import yaml
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Relative paths resolve against this file's directory, so the same config works on the
    # user's macOS checkout and in the Linux sandbox where the same folders are mounted
    # elsewhere. Absolute paths and ~ are honoured as written.
    resolved = {}
    for key, raw in cfg["paths"].items():
        q = pathlib.Path(os.path.expanduser(raw))
        resolved[key] = str(q.resolve() if q.is_absolute() else (_ROOT / q).resolve())
    cfg["paths"] = resolved
    # The frozen system's model ids are also honoured from the environment, so the runner picks
    # up exactly what the product would use if these are set there.
    cfg["models"]["extraction"] = os.getenv("GEMINI_MODEL", cfg["models"]["extraction"])
    cfg["models"]["judge"] = os.getenv("GEMINI_JUDGE_MODEL", cfg["models"]["judge"])
    return cfg


def mask(value: str | None) -> str:
    """Safe-to-print form of a secret: length and last 4 only."""
    if not value:
        return "<unset>"
    return f"<{len(value)} chars, ends …{value[-4:]}>"
