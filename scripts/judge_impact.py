#!/usr/bin/env python3
"""The judge's impact on one run: all scored fields combined, then per field.

    python scripts/judge_impact.py runs/sroie_full958
    python scripts/judge_impact.py runs/sroie_test337 runs/sroie_train621 --criterion contains

results.json already carries `judge.fields_fixed` / `fields_harmed` / `net_lift_fields`, but a
field count answers "how many verdicts moved", not "how much better is the output". This adds
the three things a reader asks next and results.json does not hold:

  * the accuracy delta with a PAIRED interval. The arms score the same documents, so resampling
    them independently and subtracting the two intervals overstates the uncertainty badly --
    the two arms agree on ~99% of fields and that agreement has to be kept inside the resample.
    Resamples documents once and recomputes BOTH arms on the same draw.
  * relative error reduction. +0.61pp reads as noise; "4.2% of the remaining errors" is the
    same fact at the scale the reader cares about.
  * how many DOCUMENTS the judge touched, split into net-better and net-worse. A net of +29
    fields is compatible with 29 documents improved and with 48 improved against 19 damaged,
    and those are different products.

The baseline is RAW_POSTPROCESSED, never RAW: RAW carries no `normalizedValue`, so measuring
from it credits the judge with the product's number parser. Falls back to RAW with a warning
when the run has no RAW_POSTPROCESSED arm, because that is the number people quote by mistake.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import random
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import doctypes                                                          # noqa: E402
from core.canonical import read_jsonl                                    # noqa: E402
from core.metrics import (ACCURACY_DEN, _gt_dir, load_rule_registry,   # noqa: E402
                          score_document, scoring_policy)
from core.normalize import set_numeric_source                            # noqa: E402

CRITERIA = {"exact": "ok", "contains": "ok_contained", "lenient": "ok_lenient",
            "conv-adj": "ok_convention"}


def rows_by_arm(run: pathlib.Path):
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    dataset = manifest.get("dataset")
    gt = {r.doc_id: r for r in read_jsonl(str(_gt_dir(spec.name, dataset) / "ground_truth.jsonl"))}
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    set_numeric_source(scoring_policy(None)["numeric_source"])

    out = collections.defaultdict(list)
    for f in sorted((run / "raw").glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        if not rec.get("ok"):
            continue
        g = gt.get(rec["doc_id"])
        if g is None:
            continue
        for arm, data in (rec.get("arms") or {}).items():
            out[arm].extend(score_document(g, data, rules, spec))
    return out


def paired_delta_ci(b, a, keys, ok_key, iters=2000, seed=20260901):
    """95% interval on (accuracy_after - accuracy_before), resampling DOCUMENTS once per draw
    and recomputing both arms on that same draw. Returns (lo, hi, share_positive)."""
    by_doc = collections.defaultdict(list)
    for k in keys:
        by_doc[k[0]].append(k)
    docs = list(by_doc)
    if len(docs) < 2:
        return None, None, None
    rng = random.Random(seed)
    samples = []
    for _ in range(iters):
        pick = [rng.choice(docs) for _ in docs]
        nb = na = n = 0
        for d in pick:
            for k in by_doc[d]:
                n += 1
                nb += bool(b[k][ok_key])
                na += bool(a[k][ok_key])
        if n:
            samples.append((na - nb) / n)
    if len(samples) < 50:
        return None, None, None
    samples.sort()
    return (samples[int(.025 * len(samples))], samples[int(.975 * len(samples)) - 1],
            sum(s > 0 for s in samples) / len(samples))


def report(run: pathlib.Path, criterion: str, iters: int) -> None:
    rows = rows_by_arm(run)
    if "FINAL" not in rows:
        print(f"  !! {run.name} has no FINAL arm"); return
    baseline = "RAW_POSTPROCESSED" if "RAW_POSTPROCESSED" in rows else "RAW"
    if baseline == "RAW":
        print("  !! no RAW_POSTPROCESSED arm; measuring from RAW, which credits the judge with "
              "the product's number parser. Back-fill with scripts/add_raw_pp_arm.py.")
    ok_key = CRITERIA[criterion]

    b = {(r["doc_id"], r["path"]): r for r in rows[baseline]}
    a = {(r["doc_id"], r["path"]): r for r in rows["FINAL"]}
    # metrics.ACCURACY_DEN, not "every row both arms carry": a correct null and a
    # hallucination are reported on their own lines and are in no accuracy denominator.
    # Applying the same filter here is what keeps these numbers equal to results.json's.
    keys = [k for k in b if k in a and b[k]["state"] in ACCURACY_DEN
            and a[k]["state"] in ACCURACY_DEN]
    n = len(keys)
    if not n:
        print(f"  !! {run.name}: no fields scored in both arms"); return

    nb = sum(bool(b[k][ok_key]) for k in keys)
    na = sum(bool(a[k][ok_key]) for k in keys)
    fixed = sum((not b[k][ok_key]) and a[k][ok_key] for k in keys)
    harmed = sum(b[k][ok_key] and (not a[k][ok_key]) for k in keys)
    lo, hi, pos = paired_delta_ci(b, a, keys, ok_key, iters=iters)

    docs = {k[0] for k in keys}
    per_doc = collections.Counter()
    for d in docs:
        ks = [k for k in keys if k[0] == d]
        f = sum((not b[k][ok_key]) and a[k][ok_key] for k in ks)
        h = sum(b[k][ok_key] and (not a[k][ok_key]) for k in ks)
        if f or h:
            per_doc["better" if f > h else "worse" if h > f else "neutral"] += 1

    print(f"\n===== {run.name}: judge + refinement, {baseline} -> FINAL, criterion={criterion}")
    print(f"  documents {len(docs)}   scored field instances {n}")
    print(f"  accuracy            {nb / n * 100:6.2f}%  ->  {na / n * 100:6.2f}%"
          f"   ({(na - nb) / n * 100:+.2f} pp)")
    if lo is not None:
        print(f"  delta 95% CI        [{lo * 100:+.2f}, {hi * 100:+.2f}] pp   "
              f"({pos * 100:.1f}% of resamples positive)")
    print(f"  fields fixed        {fixed}")
    print(f"  fields harmed       {harmed}")
    print(f"  net                 {fixed - harmed:+d}")
    print(f"  harm rate           {harmed / max(nb, 1) * 100:.2f}%  (share of fields correct "
          f"in {baseline} that FINAL got wrong)")
    print(f"  errors              {n - nb} -> {n - na}   relative error reduction "
          f"{(1 - (n - na) / max(n - nb, 1)) * 100:.1f}%")
    touched = sum(per_doc.values())
    print(f"  documents touched   {touched} of {len(docs)} ({touched / len(docs) * 100:.1f}%)"
          f"   net-better {per_doc['better']}, net-worse {per_doc['worse']}, "
          f"offsetting {per_doc['neutral']}")

    print(f"\n  {'path':40s} {'n':>5s} {'before':>8s} {'after':>8s} {'delta':>8s} "
          f"{'fixed':>6s} {'harmed':>7s}")
    for p in sorted({k[1] for k in keys}):
        ks = [k for k in keys if k[1] == p]
        pn = len(ks)
        pb = sum(bool(b[k][ok_key]) for k in ks)
        pa = sum(bool(a[k][ok_key]) for k in ks)
        pf = sum((not b[k][ok_key]) and a[k][ok_key] for k in ks)
        ph = sum(b[k][ok_key] and (not a[k][ok_key]) for k in ks)
        print(f"  {p:40s} {pn:5d} {pb / pn * 100:7.2f}% {pa / pn * 100:7.2f}% "
              f"{(pa - pb) / pn * 100:+7.2f}  {pf:6d} {ph:7d}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="+", help="one or more runs/<id> directories")
    ap.add_argument("--criterion", default="exact", choices=sorted(CRITERIA),
                    help="which criterion to measure the lift under (default: exact)")
    ap.add_argument("--iters", type=int, default=2000, help="bootstrap resamples")
    a = ap.parse_args(argv)
    for name in a.run:
        run = pathlib.Path(name)
        if not run.is_absolute():
            run = _ROOT / run
        if not (run / "raw").is_dir():
            print(f"!! {name} is not a run directory"); return 2
        report(run, a.criterion, a.iters)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
