#!/usr/bin/env python3
"""Reconstruct the un-stored intermediate pipeline states, for free.

The benchmark scores TWO arms — RAW (extraction + extraction_postprocessing, the judge's
input) and FINAL (RAW + judge + refinement + postprocessing, the shipped output). The two
states in between are deliberately not stored as arms:

    bare extraction                     runs/<id>/stages/<doc>/01_extract_raw.json
    refined, before postprocessing      runs/<id>/stages/<doc>/04_refined.json

Both are already written to disk by the runner, and both are *deterministic*: bare extraction
is cached under _cache/, and refinement is a pure function of (RAW, judge report), which are
also both kept. So nothing is lost by not scoring them — and if a question later needs them,
this script rebuilds a scoreable runs/<id>-derived/ from the artifacts on disk without a
single model call.

    python scripts/derive_arms.py --run runs/main --arm EXTRACT
    python scripts/derive_arms.py --run runs/main --arm REFINED
    python scripts/derive_arms.py --run runs/main --arm EXTRACT --arm REFINED --out runs/main-derived

Then score it exactly like any other run:

    python steps/step7_metrics.py runs/main-derived

Why this is not the default: every extra scored arm doubles the report's surface and invites
the reader to compare stages that never both exist in production. RAW and FINAL are the two
states the product actually persists (pages.$.extraction_postprocessing_result and
pages.$.postprocessing_result). The rest is diagnosis, run on demand.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

STAGE_OF_ARM = {"EXTRACT": "01_extract_raw.json", "REFINED": "04_refined.json"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="an existing runs/<id> directory")
    ap.add_argument("--arm", action="append", default=[], choices=sorted(STAGE_OF_ARM),
                    help="intermediate to add; repeatable (default: both)")
    ap.add_argument("--out", default=None, help="default: <run>-derived")
    a = ap.parse_args(argv)

    run = pathlib.Path(a.run)
    if not run.is_absolute():
        run = _ROOT / run
    stages, raw = run / "stages", run / "raw"
    if not raw.is_dir():
        print(f"!! {raw} does not exist — is that a run directory?")
        return 2
    if not stages.is_dir():
        print(f"!! {stages} does not exist. Per-stage files were added on 2026-09-02; a run "
              f"made before that kept only the scored arms and cannot be back-filled.")
        return 2

    arms = a.arm or sorted(STAGE_OF_ARM)
    out = pathlib.Path(a.out) if a.out else run.parent / f"{run.name}-derived"
    if not out.is_absolute():
        out = _ROOT / out
    (out / "raw").mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "coverage.json"):
        if (run / name).exists():
            shutil.copy2(run / name, out / name)

    n_docs = 0
    missing: dict[str, int] = {}
    for f in sorted(raw.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        doc_dir = stages / rec["doc_id"]
        for arm in arms:
            sf = doc_dir / STAGE_OF_ARM[arm]
            if not sf.exists():
                missing[arm] = missing.get(arm, 0) + 1
                continue
            rec.setdefault("arms", {})[arm] = json.loads(sf.read_text(encoding="utf-8"))["data"]
        (out / "raw" / f.name).write_text(json.dumps(rec, ensure_ascii=False, default=str),
                                          encoding="utf-8")
        n_docs += 1

    print(f"{n_docs} documents -> {out}")
    print(f"arms now present: RAW, FINAL, {', '.join(arms)}")
    for arm, n in sorted(missing.items()):
        print(f"!! {arm}: {n} documents had no {STAGE_OF_ARM[arm]} (the stage never ran there)")
    print(f"\nscore it with:  python steps/step7_metrics.py {out.relative_to(_ROOT)}")
    print("note: step7's judge section still compares RAW -> FINAL; the derived arms appear "
          "in the per-arm tables only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
