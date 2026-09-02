#!/usr/bin/env python3
"""Fail if the harness's pins drift from docuxray_ai_backend/requirements.txt.

The harness imports the production modules directly. If it runs a different version of
pydantic or google-genai than the product does, it is benchmarking a different stack -- quietly.
This is run as a test, so drift breaks the build rather than surprising someone mid-run.
"""
from __future__ import annotations
import pathlib, sys, re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
AI_BACKEND = _ROOT / ".." / "docuxray_ai_backend" / "requirements.txt"

# Needed by the harness itself, not by the product. No pin to match.
HARNESS_ONLY = {"pyyaml", "pytest"}

# Packages whose version cannot affect any measured value, with the reason. A drift here is
# reported but does not fail. NOTHING is added to this list without an argument for why the
# package is never executed on the benchmark's path.
PIN_EXCEPTIONS = {
    "pymupdf": ("imported by ai/extraction/pdf_utils.py at module load, but fitz is only called "
                "from pdf_to_png_paths() via the mime_type=='application/pdf' branch, which the "
                "benchmark never enters (JPG input). Import-satisfaction only. "
                "No 1.24.0 wheel exists for CPython 3.13 on macOS arm64."),
}


def parse(path: pathlib.Path) -> dict:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()          # drop inline comments
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s]+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def check() -> list:
    if not AI_BACKEND.exists():
        return [f"cannot find {AI_BACKEND}"]
    ab, mine = parse(AI_BACKEND), parse(_ROOT / "requirements.txt")
    problems = []
    for name, version in sorted(mine.items()):
        if name in HARNESS_ONLY:
            continue
        if name not in ab:
            problems.append(f"{name}=={version} is not in ai_backend/requirements.txt — "
                            f"either it is harness-only (add it to HARNESS_ONLY) or the pin is wrong")
        elif ab[name] != version and name not in PIN_EXCEPTIONS:
            problems.append(f"{name}: harness pins {version}, ai_backend pins {ab[name]} — "
                            f"match ai_backend, never the other way round")
    # A pin that is present in ai_backend but absent from the harness is fine only if the
    # harness genuinely does not import it; that is settled by the import-graph walk, not here.
    return problems


def warnings_() -> list:
    """Drift that is allowed, each with its stated reason."""
    ab, mine = parse(AI_BACKEND), parse(_ROOT / "requirements.txt")
    out = []
    for name, why in PIN_EXCEPTIONS.items():
        if name in mine and name in ab and mine[name] != ab[name]:
            out.append(f"{name}: {mine[name]} vs ai_backend {ab[name]} — allowed: {why}")
        elif name not in mine:
            out.append(f"{name}: unpinned in the harness — allowed: {why}")
    return out


if __name__ == "__main__":
    probs = check()
    ab, mine = parse(AI_BACKEND), parse(_ROOT / "requirements.txt")
    print(f"harness pins {len(mine)} packages; {len(mine) - len(HARNESS_ONLY & set(mine))} "
          f"must match ai_backend\n")
    for name, v in sorted(mine.items()):
        tag = "harness-only" if name in HARNESS_ONLY else ("matches ai_backend" if ab.get(name) == v
                                                           else f"DRIFT (ai_backend: {ab.get(name)})")
        print(f"  {name+'=='+v:28s} {tag}")
    print()
    for w in warnings_():
        print("  ~~ allowed drift:", w)
    for p in probs:
        print("  !!", p)
    print("PINS OK" if not probs else f"{len(probs)} PIN PROBLEM(S)")
    sys.exit(0 if not probs else 1)
