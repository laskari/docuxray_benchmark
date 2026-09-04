#!/usr/bin/env python3
"""Re-derive the FINAL arm of an existing run from its stored refined data. No model calls.

    python3 scripts/rebuild_final.py --run runs/probe

The postprocessing tail is a pure function of `04_refined.json`, and the runner writes that
file for every document. So whenever the tail changes -- a postprocessor fix, or the
2026-09-03 addition of the format contract that `core/runner.py` had been skipping -- the
corrected FINAL arm for a completed run costs nothing to obtain. Re-running the pipeline to
get it would re-pay for extraction and the judge, and would also change the extraction and
judge outputs, which is the opposite of what you want when the question is specifically
"what did the tail change?".

Non-destructive: writes a new run directory, default `<run>-retail`, leaving the original as
the record of what was measured before. Score it like any other run:

    python3 steps/step7_metrics.py runs/probe-retail

What this CANNOT re-derive is anything upstream of the tail. A change to the extraction
prompt, the model, or `run.image_prep` moves 01/03/04 and needs a real run.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


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
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _diff(old: dict, new: dict) -> list[tuple[str, object, object]]:
    lo, ln = dict(leaves(old)), dict(leaves(new))
    return [(k, lo.get(k, ABSENT), ln.get(k, ABSENT))
            for k in sorted(set(lo) | set(ln))
            if _num(lo.get(k, ABSENT)) != _num(ln.get(k, ABSENT))]


def _stored_final(doc_dir: pathlib.Path) -> tuple[pathlib.Path, dict] | tuple[None, None]:
    """The 05 dump, whatever it is named. Some were written with a doc_id prefix."""
    hits = sorted(doc_dir.glob("*05_postprocessed.json"))
    if not hits:
        return None, None
    return hits[0], json.loads(hits[0].read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="an existing runs/<id> directory")
    ap.add_argument("--out", default=None, help="default: <run>-retail")
    ap.add_argument("--doc-type", default=None,
                    help="default: read from the run's manifest, else config.yaml")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    a = ap.parse_args(argv)

    from ai.pipeline_core import TAIL_VERSION, postprocess_page

    run = pathlib.Path(a.run)
    if not run.is_absolute():
        run = _ROOT / run
    stages, raw = run / "stages", run / "raw"
    if not stages.is_dir() or not raw.is_dir():
        print(f"!! {run} is missing stages/ or raw/ — is that a run directory?")
        return 2

    manifest = {}
    if (run / "manifest.json").exists():
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    doc_type = a.doc_type or manifest.get("doc_type")
    if not doc_type:
        from config import load
        doc_type = load()["run"]["doc_type"]

    out = pathlib.Path(a.out) if a.out else run.parent / f"{run.name}-retail"
    if not out.is_absolute():
        out = _ROOT / out
    if not a.dry_run:
        (out / "raw").mkdir(parents=True, exist_ok=True)
        for name in ("coverage.json",):
            if (run / name).exists():
                shutil.copy2(run / name, out / name)

    n_docs = n_changed = n_skipped = 0
    changed_paths: dict[str, int] = {}
    contract_hits: dict[str, int] = {}
    report: list[dict] = []

    for f in sorted(raw.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        doc_id = rec["doc_id"]
        doc_dir = stages / doc_id
        refined_file = doc_dir / "04_refined.json"
        if not refined_file.exists():
            # A document that timed out in the judge has no refined state to re-derive
            # from. Carrying its old FINAL forward would silently mix tail versions, so
            # it is dropped from the rebuilt run and counted.
            n_skipped += 1
            continue

        refined = json.loads(refined_file.read_text(encoding="utf-8"))["data"]
        outcome = postprocess_page(refined, doc_type, job_id=None)

        stored_path, stored = _stored_final(doc_dir)
        delta = _diff(stored["data"], outcome.data) if stored else []
        if delta:
            n_changed += 1
            for path, *_ in delta:
                changed_paths[path] = changed_paths.get(path, 0) + 1
        for p in outcome.format_validation["invalid_paths"]:
            contract_hits[p] = contract_hits.get(p, 0) + 1
        report.append({"doc_id": doc_id, "changed": [
            {"path": p, "was": o, "now": n} for p, o, n in delta],
            "format_invalid": outcome.format_validation["invalid_paths"]})

        n_docs += 1
        if a.dry_run:
            continue

        rec.setdefault("arms", {})["FINAL"] = outcome.data
        (out / "raw" / f.name).write_text(
            json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")
        d = out / "stages" / doc_id
        d.mkdir(parents=True, exist_ok=True)
        body = dict(stored or {})
        body.update({
            "doc_id": doc_id, "stage": "postprocess", "arm": "FINAL",
            "note": f"re-derived by rebuild_final.py from 04_refined.json, "
                    f"tail_version={TAIL_VERSION}; no model call",
            "tail_version": TAIL_VERSION,
            "warnings": outcome.warnings,
            "format_validation": outcome.format_validation,
            "data": outcome.data,
        })
        (d / (stored_path.name if stored_path else "05_postprocessed.json")).write_text(
            json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        shutil.copy2(refined_file, d / "04_refined.json")

    # ------------------------------------------------------------------ report
    print(f"\n{n_docs} documents re-derived from stored refined data (0 model calls)")
    if n_skipped:
        print(f"{n_skipped} dropped: no 04_refined.json (the stage never ran there)")
    print(f"tail_version: {TAIL_VERSION}")
    print(f"\nFINAL arm changed on {n_changed} of {n_docs} documents.")
    if changed_paths:
        print("\n  fields that moved (documents affected):")
        for path, n in sorted(changed_paths.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {n:4d}  {path}")
    if contract_hits:
        print("\n  format-contract violations found (documents affected):")
        for path, n in sorted(contract_hits.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {n:4d}  {path}")
    else:
        print("\n  the format contract found no violations in this run — the step the tail was "
              "\n  missing does not change these documents' numbers.")

    if a.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    manifest.update({
        "run_id": out.name,
        "derived_from": str(run.relative_to(_ROOT)) if run.is_relative_to(_ROOT) else str(run),
        "derived_by": "scripts/rebuild_final.py",
        "tail_version": TAIL_VERSION,
        "n_run": n_docs,
        "n_final_changed_vs_source": n_changed,
        "note": "FINAL re-derived from stored 04_refined.json. Extraction and judge outputs "
                "are the source run's, unchanged — this says nothing about a tail change's "
                "effect on the model stages.",
    })
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                       encoding="utf-8")
    (out / "rebuild_final_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    rel = out.relative_to(_ROOT) if out.is_relative_to(_ROOT) else out
    print(f"\nwritten to {rel}")
    print(f"score it with:  python3 steps/step7_metrics.py {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
