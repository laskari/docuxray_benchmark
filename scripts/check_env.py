#!/usr/bin/env python3
"""Verify the run environment WITHOUT revealing the key.

Prints only a masked form (length + last 4). Exits non-zero if anything is missing, so it can
gate the runner.

    python3 scripts/check_env.py
"""
from __future__ import annotations
import pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import os                      # noqa: E402

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    print("!! the venv is missing its packages (no 'yaml').\n"
          "   pip install almost certainly aborted -- if one package has no wheel for this\n"
          "   interpreter, pip installs NOTHING and the venv is left empty.\n\n"
          "   fix:  ./setup.sh          (it now falls back automatically and verifies)\n"
          "   or :  PYTHON=python3.11 ./setup.sh    (exact pins, matches ai_backend)\n")
    raise SystemExit(1)

from config import load, load_env, mask, SECRET_KEYS   # noqa: E402

ok = True
names = load_env()
print(f".env                 {'found' if (_ROOT / '.env').exists() else 'MISSING — cp .env.example .env'}")
print(f"  keys set           {names or '(none)'}")

key = next((os.environ[k] for k in SECRET_KEYS if os.environ.get(k)), None)
print(f"  GEMINI_API_KEY     {mask(key)}")
if not key:
    ok = False
    print("    -> no API key. Put it in .env; it is gitignored and never printed.")

cfg = load()
print(f"\nmodels")
print(f"  extraction         {cfg['models']['extraction']}")
print(f"  judge              {cfg['models']['judge']}")
print(f"spend cap            ${cfg['run']['spend_cap_usd']:.2f}")

for name, path in cfg["paths"].items():
    exists = pathlib.Path(path).exists()
    ok = ok and exists
    print(f"path {name:15s} {'ok  ' if exists else 'MISSING '}{path}")

sys.path.insert(0, cfg["paths"]["ai_backend"])
for mod in ("ai.extraction.gemini_extraction_service",
            "ai.judge.core.judge", "ai.refinement.pipeline", "shared.pricing"):
    try:
        __import__(mod); print(f"import {mod:45s} ok")
    except Exception as exc:
        ok = False; print(f"import {mod:45s} FAILED: {type(exc).__name__}: {exc}")

print("\n" + ("READY" if ok else "NOT READY — fix the items above"))
sys.exit(0 if ok else 1)
