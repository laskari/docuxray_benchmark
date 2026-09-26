#!/usr/bin/env python3
"""What actually differs between the arms' PAYLOADS, as opposed to their verdicts.

    python scripts/probe_arm_payload_delta.py runs/sroie_full958

Two questions, because the arm table answers neither and a reader who assumes the answer gets
the judge's lift wrong:

  1. Does arm RAW manipulate values?  RAW is extraction + run_extraction_postprocessing, so
     "RAW" is not bare model output and the name invites the assumption that it is. Diffs
     01_extract_raw.json -> 02_extraction_postprocessing.json leaf by leaf and classifies every
     difference as STRUCTURAL (a null key removed, a leaf re-expanded as {"originalValue":
     null}) or a VALUE manipulation. On SROIE, 958 documents: 44,331 structural, 0 value.
     That is the evidence for "RAW compares the model's own reading against ground truth".

  2. What does the type postprocessor do to RAW?  Diffs arm RAW -> arm RAW_POSTPROCESSED and
     splits the delta into the added `normalizedValue` key (which RAW cannot carry --
     NumericValue sets extra="forbid") and everything else. The distinction matters because the
     two arms then read different keys through different parsers, and the arms tying is only
     meaningful once you know the payload delta was a key addition rather than a no-op.

Reads only what is already on disk. No model call, nothing written.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

_MISSING = object()
_EMPTY_LIST = "<<EMPTY_LIST>>"


def leaves(obj, path=""):
    """Flatten to scalar leaves. A NumericValue's two sub-keys stay separate on purpose: the
    whole question is which of them changed."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        if not obj:
            yield path, _EMPTY_LIST
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def _generic(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def extraction_postprocessing_delta(run: pathlib.Path):
    """01 -> 02. Returns (n_docs, structural Counter, value Counter, examples)."""
    structural, value = collections.Counter(), collections.Counter()
    examples = collections.defaultdict(list)
    n = 0
    for d in sorted((run / "stages").iterdir() if (run / "stages").is_dir() else []):
        before, after = d / "01_extract_raw.json", d / "02_extraction_postprocessing.json"
        if not (before.exists() and after.exists()):
            continue
        n += 1
        A = dict(leaves(json.loads(before.read_text(encoding="utf-8"))["data"]))
        B = dict(leaves(json.loads(after.read_text(encoding="utf-8"))["data"]))
        for k in set(A) | set(B):
            va, vb = A.get(k, _MISSING), B.get(k, _MISSING)
            if va == vb:
                continue
            g = _generic(k)
            # Structural shapes -- the schema cleanup, in matched pairs.
            if va is _MISSING and vb is None:
                structural[f"key ADDED as null | {g}"] += 1; continue
            if vb is _MISSING and va is None:
                structural[f"null key REMOVED | {g}"] += 1; continue
            if va == _EMPTY_LIST and vb is None:
                structural[f"[] -> null | {g}"] += 1; continue
            if va is None and vb == _EMPTY_LIST:
                structural[f"null -> [] | {g}"] += 1; continue
            if vb is _MISSING and va == _EMPTY_LIST:
                structural[f"[] REMOVED | {g}"] += 1; continue
            if va is _MISSING and vb == _EMPTY_LIST:
                structural[f"[] ADDED | {g}"] += 1; continue
            if k.endswith(".originalValue"):
                parent = A.get(k[: -len(".originalValue")], _MISSING)
                if parent == vb:                        # scalar wrapped, same scalar
                    structural[f"scalar WRAPPED into originalValue | {g}"] += 1; continue
                if parent is _MISSING and vb is None:   # leaf expanded to a null NumericValue
                    structural[f"leaf EXPANDED to originalValue:null | {g}"] += 1; continue
            # Anything left touched a value.
            if vb is _MISSING:      value[f"VALUE DROPPED | {g}"] += 1
            elif va is _MISSING:    value[f"VALUE INVENTED | {g}"] += 1
            elif vb is None:        value[f"VALUE NULLED | {g}"] += 1
            elif va is None:        value[f"null -> VALUE | {g}"] += 1
            else:                   value[f"VALUE CHANGED | {g}"] += 1
            if len(examples[g]) < 3:
                examples[g].append((d.name, k, va, vb))
    return n, structural, value, examples


def postprocessor_delta(run: pathlib.Path):
    """arm RAW -> arm RAW_POSTPROCESSED."""
    added_nv = 0
    buckets = {k: collections.Counter() for k in
               ("originalValue REWRITTEN", "value NULLED", "other value CHANGED",
                "empty list -> null", "other key ADDED")}
    n = 0
    for f in sorted((run / "raw").glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        arms = rec.get("arms") or {}
        if "RAW" not in arms or "RAW_POSTPROCESSED" not in arms:
            continue
        n += 1
        A = dict(leaves(arms["RAW"])); B = dict(leaves(arms["RAW_POSTPROCESSED"]))
        for k in set(A) | set(B):
            va, vb = A.get(k, _MISSING), B.get(k, _MISSING)
            if va == vb:
                continue
            g = _generic(k)
            if va is _MISSING and k.endswith(".normalizedValue"):
                added_nv += 1
            elif va == _EMPTY_LIST and vb is None:
                buckets["empty list -> null"][g] += 1
            elif va is _MISSING:
                buckets["other key ADDED"][g] += 1
            elif vb is None or vb is _MISSING:
                buckets["value NULLED"][g] += 1
            elif k.endswith(".originalValue"):
                buckets["originalValue REWRITTEN"][g] += 1
            else:
                buckets["other value CHANGED"][g] += 1
    return n, added_nv, buckets


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="+", help="one or more runs/<id> directories")
    ap.add_argument("--top", type=int, default=12, help="paths to list per bucket")
    a = ap.parse_args(argv)

    rc = 0
    for name in a.run:
        run = pathlib.Path(name)
        if not run.is_absolute():
            run = _ROOT / run
        print(f"\n===== {name}")
        if not (run / "raw").is_dir():
            print("  !! not a run directory"); rc = 2; continue

        n, structural, value, examples = extraction_postprocessing_delta(run)
        if not n:
            print("  01_extract_raw.json / 02_extraction_postprocessing.json not on disk -- "
                  "this run predates per-stage dumps; question 1 cannot be answered.")
        else:
            print(f"\n  [1] arm RAW: does extraction_postprocessing manipulate values?  "
                  f"({n} documents)")
            print(f"      STRUCTURAL differences : {sum(structural.values())}")
            for k, c in structural.most_common(a.top):
                print(f"          {c:7d}  {k}")
            print(f"      VALUE manipulations    : {sum(value.values())}"
                  f"{'   <-- RAW is NOT value-untouched' if value else '   (none)'}")
            for k, c in value.most_common(a.top):
                print(f"          {c:7d}  {k}")
                for doc, path, va, vb in examples[k.split(' | ', 1)[-1]][:2]:
                    show = lambda v: "<<MISSING>>" if v is _MISSING else json.dumps(v, ensure_ascii=False)[:70]
                    print(f"              {doc}  {path}:  {show(va)}  ->  {show(vb)}")
            if value:
                rc = 1

        n2, added_nv, buckets = postprocessor_delta(run)
        print(f"\n  [2] arm RAW -> RAW_POSTPROCESSED: what the type postprocessor did  "
              f"({n2} documents)")
        print(f"      normalizedValue ADDED beside an unchanged originalValue : {added_nv}")
        for label, c in buckets.items():
            print(f"      {label:40s}: {sum(c.values())}")
            for k, v in c.most_common(a.top if sum(c.values()) <= 40 else 5):
                print(f"          {v:7d}  {k}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
