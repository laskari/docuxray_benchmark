#!/usr/bin/env python3
"""Back-fill the RAW_POSTPROCESSED arm into a run that predates it.

    python scripts/add_raw_pp_arm.py runs/main
    python scripts/add_raw_pp_arm.py runs/main --force        # recompute existing ones

RAW_POSTPROCESSED = the same ai.pipeline_core.postprocess_page that ends arm FINAL, applied to
RAW instead of to refined data. Free: no model call, nothing cached, nothing published. It is a
SCORING CONSTRUCT -- production never builds this document.

It exists because NumericValue declares only `originalValue` and sets extra="forbid", so arm
RAW is scored by parsing printed strings with the benchmark's own parser while arm FINAL reads
`normalizedValue` from the product's. RAW -> FINAL was therefore never a clean
before/after-judge comparison. On runs/main, 65 of 125 apparent "judge fixes" were that parser
difference alone.

core/runner.py now emits this arm during the run, so new runs never need this script. What it
is still for is the runs already on disk -- and the one rule it enforces is that a run comes
out either fully armed or not at all. A PARTIALLY armed run is worse than an unarmed one: it
scores its arms on different denominators, which is exactly how runs/main came to publish RAW
and FINAL at n=1,000 beside RAW_POSTPROCESSED at n=969 in the same results.md.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import load, load_env                                        # noqa: E402

ARM = "RAW_POSTPROCESSED"
STAGE_FILE = "02b_raw_postprocessed.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", help="an existing runs/<id> directory")
    ap.add_argument("--force", action="store_true",
                    help="recompute the arm on documents that already carry it (use after a "
                         "postprocessor change; the arm is not cached)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change and write nothing")
    a = ap.parse_args(argv)

    run = pathlib.Path(a.run)
    if not run.is_absolute():
        run = _ROOT / run
    raw_dir = run / "raw"
    if not raw_dir.is_dir():
        print(f"!! {raw_dir} does not exist")
        return 2

    load_env()
    cfg = load()
    sys.path.insert(0, cfg["paths"]["ai_backend"])

    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cfg["run"]["doc_type"] = manifest.get("doc_type", cfg["run"]["doc_type"])

    from core.runner import Runner
    runner = Runner(cfg, ["RAW"], run.name)

    files = sorted(raw_dir.glob("*.json"))
    added, refreshed, already, no_raw, not_ok, failed = 0, 0, 0, [], [], []
    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        doc_id = rec.get("doc_id") or f.stem
        arms = rec.get("arms") or {}
        if not rec.get("ok"):
            # A failed document is outside every denominator, so it does not need the arm --
            # but say so, because "969 of 1,000" has to be explainable.
            not_ok.append(doc_id)
            continue
        if "RAW" not in arms:
            no_raw.append(doc_id)
            continue
        if ARM in arms and not a.force:
            already += 1
            continue
        if a.dry_run:
            added += ARM not in arms
            refreshed += ARM in arms
            continue
        try:
            outcome = runner._postprocess_outcome(arms["RAW"])
        except Exception as exc:                                         # noqa: BLE001
            failed.append(f"{doc_id}: {type(exc).__name__}: {exc}")
            continue
        was_present = ARM in arms
        arms[ARM] = outcome.data
        rec["arms"] = arms
        f.write_text(json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")

        # Match what a fresh run writes, so a back-filled run and a native one are
        # indistinguishable on disk. Only where the stage directory already exists: this
        # script never invents a stages/ tree.
        doc_dir = run / "stages" / doc_id
        if doc_dir.is_dir():
            (doc_dir / STAGE_FILE).write_text(json.dumps({
                "doc_id": doc_id,
                "stage": "raw_postprocessed",
                "arm": ARM,
                "backfilled_by": "scripts/add_raw_pp_arm.py",
                "note": "SCORING CONSTRUCT, never shipped and never shown to a model: the "
                        "same ai.pipeline_core.postprocess_page arm FINAL ends with, applied "
                        "to RAW instead of to refined data",
                "warnings": outcome.warnings,
                "format_validation": outcome.format_validation,
                "data": outcome.data,
            }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        refreshed += was_present
        added += not was_present
        if (added + refreshed) % 100 == 0:
            print(f"  {added + refreshed} of {len(files)}...")

    armed = added + refreshed + already
    print(f"\n{run.name}: {len(files)} raw documents")
    print(f"  arm present after this run : {armed}")
    print(f"    newly added              : {added}")
    print(f"    recomputed (--force)     : {refreshed}")
    print(f"    already present, skipped : {already}")
    if not_ok:
        print(f"  failed documents, skipped  : {len(not_ok)}  (outside every denominator)")
    if no_raw:
        print(f"  !! no RAW arm to derive from: {len(no_raw)}  e.g. {no_raw[:4]}")
    if failed:
        print(f"  !! postprocessor errored on : {len(failed)}")
        for line in failed[:5]:
            print(f"       {line}")

    if a.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    # The whole point of this script: a partially armed run must not be left behind.
    unarmed = len(no_raw) + len(failed)
    if unarmed:
        print(f"\n!! {unarmed} scoreable document(s) have no {ARM} arm. This run is PARTIALLY "
              f"ARMED and step7 will refuse to score it (core.metrics.ArmDenominatorMismatch) "
              f"-- which is correct. Fix the cause and re-run this script; do not reach for "
              f"--allow-arm-mismatch.")
        return 1
    print(f"\nall {armed} scoreable documents carry {ARM}. Re-score with:\n"
          f"    python steps/step7_metrics.py {a.run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
