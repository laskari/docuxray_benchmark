#!/usr/bin/env python3
"""Merge completed runs into one scoreable run. No model calls, no cost.

    python scripts/merge_runs.py runs/main_a runs/main_b --out runs/main
    python steps/step7_metrics.py runs/main --dataset fatura

Why this exists: the 1,000-document main measurement is run as two 500-document halves so the
Gemini spend can be spread. Scoring each half separately answers a different question from
scoring the whole -- the cluster bootstrap resamples templates, and confidence intervals from
500 documents are wider than from 1,000 -- so the halves must be recombined before the
headline numbers are quoted, not averaged afterwards.

What it checks before merging, because a silently wrong merge is worse than a refused one:

  * no document appears in both runs (a duplicate would be counted twice, and cluster
    bootstrap would treat it as independent evidence)
  * both runs carry the same arms
  * both runs used the same models, prompt version, field map and image-prep mode -- a merge
    across a changed measured system is not a measurement, and is refused unless --force
  * only documents that actually succeeded (ok=true) are carried over; the counts are reported

Costs and tokens are summed. The merged manifest records both source runs and every field
that differed between them, so the provenance survives in the artifact that gets published.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import shutil
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Fields of the manifest that describe the MEASURED SYSTEM. A difference in any of these means
# the two halves are not measurements of the same thing.
_SYSTEM_KEYS = ("models", "field_map_version", "doc_type", "dataset",
                "image_prep", "image_prep_version", "prompt_version", "tail_version")


def _load(run: pathlib.Path) -> tuple[dict, dict]:
    mf = run / "manifest.json"
    if not mf.exists():
        sys.exit(f"!! {mf} does not exist — is that a completed run directory?")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    docs = {}
    for f in sorted((run / "raw").glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        docs[rec["doc_id"]] = rec
    if not docs:
        sys.exit(f"!! {run}/raw holds no documents.")
    return manifest, docs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="two or more completed run directories")
    ap.add_argument("--out", required=True, help="destination run directory")
    ap.add_argument("--force", action="store_true",
                    help="merge even if the runs measured different systems (records the "
                         "mismatch in the manifest; the result is not a clean measurement)")
    a = ap.parse_args(argv)

    runs = [(_ROOT / r if not pathlib.Path(r).is_absolute() else pathlib.Path(r))
            for r in a.runs]
    out = pathlib.Path(a.out)
    if not out.is_absolute():
        out = _ROOT / out
    if len(runs) < 2:
        sys.exit("!! give at least two runs to merge.")

    loaded = [(r, *_load(r)) for r in runs]

    # ---- system-identity check -------------------------------------------------------
    mismatches: dict[str, list] = {}
    base = loaded[0][1]
    for key in _SYSTEM_KEYS:
        vals = [m.get(key) for _, m, _ in loaded]
        if any(v != vals[0] for v in vals):
            mismatches[key] = vals
    if mismatches:
        print("!! these runs did not measure the same system:")
        for k, v in mismatches.items():
            for (r, _, _), val in zip(loaded, v):
                print(f"     {k:22} {r.name:20} {val}")
        if not a.force:
            print("\n   Merging them would produce a number that describes no single system.")
            print("   Re-run the older half against the current one, or pass --force and say")
            print("   so wherever the result is published.")
            return 2
        print("   --force: merging anyway; the mismatch is recorded in the manifest.\n")

    # ---- arms ------------------------------------------------------------------------
    arm_sets = [frozenset(next(iter(d.values())).get("arms") or {}) for _, _, d in loaded]
    if any(s != arm_sets[0] for s in arm_sets):
        print("!! the runs carry different arms:")
        for (r, _, _), s in zip(loaded, arm_sets):
            print(f"     {r.name:20} {sorted(s)}")
        return 2

    # ---- duplicates ------------------------------------------------------------------
    seen = collections.defaultdict(list)
    for r, _, docs in loaded:
        for d in docs:
            seen[d].append(r.name)
    dupes = {d: rs for d, rs in seen.items() if len(rs) > 1}
    if dupes:
        print(f"!! {len(dupes)} documents appear in more than one run, e.g.:")
        for d, rs in list(dupes.items())[:5]:
            print(f"     {d}  in  {', '.join(rs)}")
        print("\n   Counting a document twice inflates n and makes the cluster bootstrap treat")
        print("   one document as two independent observations. Fix the plans and re-merge.")
        return 2

    # ---- merge -----------------------------------------------------------------------
    (out / "raw").mkdir(parents=True, exist_ok=True)
    (out / "stages").mkdir(parents=True, exist_ok=True)
    n_ok = n_skipped = 0
    for r, _, docs in loaded:
        for doc_id, rec in docs.items():
            if not rec.get("ok"):
                n_skipped += 1
                continue
            (out / "raw" / f"{doc_id}.json").write_text(
                json.dumps(rec, ensure_ascii=False, default=str), encoding="utf-8")
            src = r / "stages" / doc_id
            if src.is_dir():
                dst = out / "stages" / doc_id
                if not dst.exists():
                    shutil.copytree(src, dst)
            n_ok += 1

    def _sum(key):
        return sum(float(m.get(key) or 0) for _, m, _ in loaded)

    tokens = collections.Counter()
    for _, m, _ in loaded:
        for k, v in (m.get("tokens") or {}).items():
            if isinstance(v, (int, float)):
                tokens[k] += int(v)

    manifest = {
        **{k: base.get(k) for k in _SYSTEM_KEYS},
        "run_id": out.name,
        "merged_from": [{"run_id": m.get("run_id") or r.name,
                         "path": str(r.relative_to(_ROOT)) if r.is_relative_to(_ROOT) else str(r),
                         "n_documents": len(d),
                         "plan": m.get("plan"),
                         "utc": m.get("utc"),
                         "total_cost_usd": m.get("total_cost_usd")}
                        for r, m, d in loaded],
        "system_mismatch": mismatches or None,
        "merged_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_ok": n_ok,
        "n_dropped_not_ok": n_skipped,
        "total_cost_usd": round(_sum("total_cost_usd"), 4),
        "tokens": dict(tokens),
        "git": base.get("git"),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str),
                                       encoding="utf-8")

    print(f"merged {len(loaded)} runs -> {out}")
    for r, m, d in loaded:
        print(f"   {r.name:22} {len(d):>5} documents   ${float(m.get('total_cost_usd') or 0):.2f}")
    print(f"   {'=':22} {n_ok:>5} documents   ${manifest['total_cost_usd']:.2f}")
    if n_skipped:
        print(f"   {n_skipped} documents dropped (ok=false); they are still in their source run.")
    rel = out.relative_to(_ROOT) if out.is_relative_to(_ROOT) else out
    print(f"\nscore it with:  python steps/step7_metrics.py {rel} --dataset "
          f"{base.get('dataset') or '<name>'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
