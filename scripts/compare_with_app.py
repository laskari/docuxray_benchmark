#!/usr/bin/env python3
"""Diff a run's FINAL arm against a JSON copied out of the running app, and attribute
every difference to the stage that caused it.

    python3 scripts/compare_with_app.py \
        --app  ~/Downloads/Template10_Instance1.json \
        --doc  Template10_Instance1 \
        --run  probe

Written because the manual version of this check is misleading. Comparing the two files by
eye produces a list of "differences in paymentTerms, customerMemo, categoryReasoning and
totals", which reads as four benchmark bugs and is in fact none: the app's JSON has passed
through a serve-layer transform the pipeline never applied, and most of the rest is one
stochastic stage disagreeing with itself between two runs. Attribution needs the run's own
per-stage dumps, which is what this script reads.

Each differing leaf is classified by where it came from:

  SERVE-LAYER    the app-backend serve boundary added or renamed it, no pipeline stage wrote
                 it: `documentType` (documents.py::_unwrap_structured_data), legacy renames.
                 Not a difference in output at all.
  FORMAT         same number, different JSON spelling (1 vs 1.0). Not a difference either.
  EXTRACTION     already different in 01_extract_raw.json, before any deterministic stage
                 ran. Two separate Gemini calls; a prompt or model change would show here as
                 EVERY document differing on the same field, whereas sampling noise shows as
                 scattered fields on scattered documents. Note that the app and this harness
                 must be fed the same pixels before this class means anything -- see
                 run.image_prep in config.yaml.
  JUDGE+REFINE   the same between app and 01_extract_raw, then changed by 04_refined.json.
                 The judge raised an issue and refinement acted on it. When the extracted
                 value was right and the refined one is wrong, this is judge HARM, and the
                 field_path will appear in 03_judge_report.json.
  TAIL           changed by the postprocessing tail. Usually a knock-on: a value the judge
                 rewrote no longer parses, so its normalizedValue goes null.

An empty SERVE-LAYER/FORMAT-only report means the two paths agree. A nonzero TAIL count that
is NOT downstream of a JUDGE+REFINE row on the same field is the one result that indicts this
harness rather than the product -- the tail is deterministic, so it should not differ.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

CLASSES = ("SERVE-LAYER", "FORMAT", "EXTRACTION", "JUDGE+REFINE", "TAIL", "UNATTRIBUTED")
# Keys the serve boundary injects that no pipeline stage writes (documents.py:233).
SERVE_ONLY_KEYS = {"documentType"}


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


ABSENT = "<absent>"


def _num(v):
    """1 and 1.0 are the same number; True is not 1."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _stage(stage_dir: pathlib.Path, filename: str) -> dict:
    """Read one stage dump's payload. Missing is not fatal: a run that timed out in the judge
    has no 04, and the fields it would have explained are simply unattributable."""
    hits = sorted(stage_dir.glob(f"*{filename}"))
    if not hits:
        return {}
    body = json.loads(hits[0].read_text(encoding="utf-8"))
    data = body.get("data", body)
    # The harness holds the unwrapped shape; be tolerant of a wrapped dump.
    if isinstance(data, dict) and len(data) == 1:
        only = next(iter(data))
        if only.endswith(("OutputData", "Data")) and isinstance(data[only], dict):
            data = data[only]
    return data or {}


def classify(doc_id: str, app: dict, run_dir: pathlib.Path) -> list[dict]:
    stage_dir = run_dir / "stages" / doc_id
    if not stage_dir.is_dir():
        sys.exit(f"no stage dumps at {stage_dir} -- check --run and --doc")

    final = _stage(stage_dir, "05_postprocessed.json")
    if not final:
        sys.exit(f"{doc_id} has no 05_postprocessed.json: arm FINAL never completed")
    extract = _stage(stage_dir, "01_extract_raw.json")
    refined = _stage(stage_dir, "04_refined.json")

    la, lb = dict(leaves(app)), dict(leaves(final))
    le, lr = dict(leaves(extract)), dict(leaves(refined))

    rows = []
    for key in sorted(set(la) | set(lb)):
        a, b = la.get(key, ABSENT), lb.get(key, ABSENT)
        if _num(a) == _num(b):
            continue

        if key.split(".")[-1] in SERVE_ONLY_KEYS and b is ABSENT:
            cls = "SERVE-LAYER"
        elif str(_num(a)) == str(_num(b)):
            cls = "FORMAT"
        else:
            e, r = le.get(key, ABSENT), lr.get(key, ABSENT)
            if not refined:
                cls = "UNATTRIBUTED"
            elif _num(e) != _num(r):
                cls = "JUDGE+REFINE"
            elif _num(e) == _num(b):
                cls = "EXTRACTION"
            else:
                cls = "TAIL"
        rows.append({"class": cls, "path": key, "app": a, "bench": b,
                     "at_extract": le.get(key, ABSENT), "at_refine": lr.get(key, ABSENT)})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", required=True, type=pathlib.Path,
                    help="JSON copied out of the app (the Raw JSON view, post-judge)")
    ap.add_argument("--doc", required=True, help="doc_id, e.g. Template10_Instance1")
    ap.add_argument("--run", default="probe", help="run id under runs/ (default: probe)")
    ap.add_argument("--json", action="store_true", help="emit the rows as JSON")
    args = ap.parse_args()

    app = json.loads(args.app.read_text(encoding="utf-8"))
    # Accept either a bare structured-data dict or a stage-dump-shaped {"data": ...}.
    if set(app) <= {"doc_id", "stage", "arm", "utc", "cached", "stage_ms", "note", "data"}:
        app = app.get("data", app)

    rows = classify(args.doc, app, _ROOT / "runs" / args.run)

    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    counts = {c: sum(1 for r in rows if r["class"] == c) for c in CLASSES}
    print(f"\n{args.doc}: {len(rows)} differing leaves (app vs run '{args.run}' arm FINAL)\n")
    for cls in CLASSES:
        for r in [r for r in rows if r["class"] == cls]:
            print(f"[{cls:12s}] {r['path']}")
            print(f"               app   = {json.dumps(r['app'], ensure_ascii=False)}")
            print(f"               bench = {json.dumps(r['bench'], ensure_ascii=False)}")
            if cls in ("JUDGE+REFINE", "TAIL"):
                print(f"               01_extract = {json.dumps(r['at_extract'], ensure_ascii=False)}")
                print(f"               04_refined = {json.dumps(r['at_refine'], ensure_ascii=False)}")

    print("\n  " + "  ".join(f"{c}={counts[c]}" for c in CLASSES if counts[c]) or "  identical")
    real = counts["EXTRACTION"] + counts["JUDGE+REFINE"] + counts["TAIL"] + counts["UNATTRIBUTED"]
    if not real:
        print("\n  The two paths agree: every difference is serve-layer or JSON formatting.")
    else:
        print(f"\n  {real} difference(s) in pipeline output. JUDGE+REFINE rows whose 01_extract "
              f"value\n  matches the app are judge harm, not harness drift -- cross-check them "
              f"against\n  03_judge_report.json. A TAIL row with no JUDGE+REFINE row on the same "
              f"field is\n  the only class that indicts this harness: the tail is deterministic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
