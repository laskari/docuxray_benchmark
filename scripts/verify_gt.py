#!/usr/bin/env python3
"""Trace a document's ground truth back to its source, hop by hop.

    python3 scripts/verify_gt.py --dataset cord --doc test-32
    python3 scripts/verify_gt.py --dataset cord --doc test-32 --run runs/cord_test_main_100
    python3 scripts/verify_gt.py --dataset cord --audit 100 --split test

Answers one question: WHERE DID THIS NUMBER COME FROM. Every hop is printed with the actual
bytes at that hop, so a disagreement can be pinned on exactly one of them:

    1. dataset root      config.yaml paths.<key>, resolved  -- which files were read at all
    2. annotation file   the raw JSON on disk, byte for byte
    3. gt_parse          the nested form CORD itself publishes (or 'rebuilt', flagged)
    4. valid_line        the word-level annotation, with is_key captions -- the audit trail
    5. our parse         which parser ran, and what it returned
    6. ground_truth.jsonl  what actually got stored and scored
    7. prediction        with --run: the model's value in each arm, and the verdict

--audit adds the two checks that catch a systematically wrong GT without opening an image:

    ARITHMETIC   sum(line totals) vs the printed subtotal, per document. A mis-read thousands
                 separator shows up here as a 1000x discrepancy, not as a rounding one.
    ROUND TRIP   re-parse the stored value and confirm it still equals what the annotation
                 says, so a parser change cannot silently invalidate a built corpus.

WHY THIS EXISTS. "The results are poor" has three possible causes -- the ground truth is wrong,
the instrument is wrong, or the model is wrong -- and they are indistinguishable from a summary
table. This makes the first one cheap to rule in or out before anyone argues about the third.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
from decimal import Decimal

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import doctypes                                                        # noqa: E402
from config import load as load_cfg                                    # noqa: E402
from core.canonical import read_jsonl                                  # noqa: E402
from registry import dataset_for, gt_dir as _gt_dir                    # noqa: E402


def _fmt(v, width=0):
    s = "None" if v is None else str(v)
    return s.rjust(width) if width else s


def trace(entry, spec, doc_id, root, run=None):
    gt_dir = _gt_dir(spec.name, entry.name)
    stored = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    rec = stored.get(doc_id)
    if rec is None:
        sys.exit(f"{doc_id!r} is not in {gt_dir / 'ground_truth.jsonl'}")

    adapter = entry.load(root)
    print(f"\n{'='*78}\nDOCUMENT  {doc_id}\n{'='*78}")

    print(f"\n[1] dataset root   config.yaml paths.{entry.root_config_key}")
    print(f"    -> {adapter.root}")
    print(f"    image          {adapter.root / rec.image_path}"
          f"   exists={ (adapter.root / rec.image_path).exists() }")

    raw = next((r for d, r in adapter.iter_raw() if d == doc_id), None)
    if raw is None:
        sys.exit(f"the adapter no longer yields {doc_id!r} from {adapter.root}")

    src = raw.get("_source_file") or ""
    print(f"\n[2] annotation     split={raw.get('split')}  "
          f"gt_parse_source={raw.get('gt_parse_source')}  {src}")

    print(f"\n[3] gt_parse (what CORD publishes)")
    print("    " + json.dumps(raw.get("gt_parse"), indent=2, ensure_ascii=False)
          .replace("\n", "\n    ")[:2400])

    vl = raw.get("valid_line") or []
    if vl:
        print(f"\n[4] valid_line captions (is_key words -- what the receipt PRINTS)")
        for line in vl:
            cap = " ".join(w.get("text", "") for w in (line.get("words") or [])
                           if w.get("is_key")).strip()
            val = " ".join(w.get("text", "") for w in (line.get("words") or [])
                           if not w.get("is_key")).strip()
            if cap or val:
                print(f"    {line.get('category',''):26s} caption={cap!r:22s} value={val!r}")

    print(f"\n[5]+[6] stored ground truth  ({gt_dir / 'ground_truth.jsonl'})")
    print(f"    cluster_id={rec.cluster_id}  keyset={rec.keyset_id}  "
          f"split={(rec.meta or {}).get('split')}  n_gt_rows={(rec.meta or {}).get('n_gt_rows')}")
    for path in sorted(rec.annotated_fields):
        v = rec.gt.get(path)
        if isinstance(v, list):
            print(f"    {path}")
            for i, row in enumerate(v):
                print(f"        [{i}] {row}")
        else:
            print(f"    {path:34s} = {v}")
    if rec.excluded_fields:
        print("    EXCLUDED (scored nowhere, with reason):")
        for k, why in sorted(rec.excluded_fields.items()):
            print(f"        {k:38s} {why}")

    if run:
        _trace_prediction(pathlib.Path(run), doc_id, rec, spec)


def _trace_prediction(run, doc_id, rec, spec):
    from core.metrics import flatten_prediction, unwrap, predicted_value
    from core.matching import load_rule_registry, compare
    f = run / "raw" / f"{doc_id}.json"
    if not f.exists():
        print(f"\n[7] prediction     {doc_id} is not in {run}")
        return
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    d = json.loads(f.read_text())
    arms = [a for a in ("RAW", "RAW_POSTPROCESSED", "FINAL") if a in (d.get("arms") or {})]
    print(f"\n[7] prediction     {run.name}   arms={arms}")
    flats = {a: flatten_prediction(unwrap(d["arms"][a], spec)) for a in arms}
    for path in sorted(p for p in rec.annotated_fields if not isinstance(rec.gt.get(p), list)):
        rule = rules.get(path)
        if rule is None:
            continue
        truth = rec.gt.get(path)
        print(f"    {path}")
        print(f"        GT      {truth}")
        for a in arms:
            pred = predicted_value(path, flats[a], spec)
            res = compare(pred, truth, rule)
            print(f"        {a:18s} {json.dumps(pred, ensure_ascii=False)[:70]:72s} "
                  f"{'match' if res.matched else 'MISMATCH'}"
                  f"{'' if res.matched else '   (' + res.reason + ')'}")


# --------------------------------------------------------------------------- audit
def audit(entry, spec, root, split=None, limit=0):
    """Corpus-level checks that catch a systematically wrong GT without opening an image."""
    gt_dir = _gt_dir(spec.name, entry.name)
    recs = read_jsonl(str(gt_dir / "ground_truth.jsonl"))
    if split:
        recs = [r for r in recs if (r.meta or {}).get("split") == split]
    if limit:
        recs = recs[:limit]
    adapter = entry.load(root)
    by_id = {d: r for d, r in adapter.iter_raw()}

    arith = collections.Counter()
    rt = collections.Counter()
    bad_rt, bad_arith = [], []
    missing_img = []

    for rec in recs:
        if not (adapter.root / rec.image_path).exists():
            missing_img.append(rec.doc_id)

        # ROUND TRIP -- rebuild the record from the annotation and compare field by field.
        raw = by_id.get(rec.doc_id)
        if raw is not None:
            fresh = adapter.build(rec.doc_id, raw)
            same = (fresh.gt == rec.gt and fresh.annotated_fields == rec.annotated_fields)
            rt["identical" if same else "DRIFTED"] += 1
            if not same and len(bad_rt) < 5:
                diff = {k for k in set(fresh.gt) | set(rec.gt) if fresh.gt.get(k) != rec.gt.get(k)}
                bad_rt.append((rec.doc_id, sorted(diff)[:4]))

        # ARITHMETIC -- sum of line totals against the printed subtotal.
        rows = rec.gt.get("lineItems") or []
        sub = None
        for r in (rec.gt.get("totals.subtotal") or []):
            if r.get("value") is not None:
                sub = Decimal(r["value"])
        if sub is None or not rows:
            arith["no subtotal or no rows"] += 1
            continue
        tot = Decimal(0)
        complete = True
        for r in rows:
            if r.get("lineTotal") is None:
                complete = False
            else:
                tot += Decimal(r["lineTotal"])
        if not complete:
            arith["rows incomplete"] += 1
            continue
        d = abs(sub - tot)
        if d <= max(Decimal("1"), sub.copy_abs() * Decimal("0.01")):
            arith["matches"] += 1
        elif tot != 0 and (abs(sub / tot - 1000) < Decimal("0.01")
                           or abs(tot / sub - 1000) < Decimal("0.01")):
            arith["OFF BY 1000x -- a separator was mis-read"] += 1
            bad_arith.append((rec.doc_id, str(tot), str(sub)))
        else:
            arith["differs (discount / tax / sub-items)"] += 1

    print(f"\n{'='*78}\nAUDIT  dataset={entry.name}  split={split or 'all'}  "
          f"n={len(recs)}\n{'='*78}")
    print("\nROUND TRIP  rebuild each record from its annotation and compare:")
    for k, v in rt.most_common():
        print(f"    {v:5d}  {k}")
    for b in bad_rt:
        print(f"           {b}")
    print("\nARITHMETIC  sum(lineItems.lineTotal) vs totals.subtotal:")
    for k, v in arith.most_common():
        print(f"    {v:5d}  {k}")
    for b in bad_arith[:5]:
        print(f"           {b}")
    print(f"\nIMAGES      {len(recs) - len(missing_img)} of {len(recs)} present"
          + (f"   MISSING: {missing_img[:5]}" if missing_img else ""))
    print("\nA 1000x line above is the signature of a mis-read thousands separator in GROUND\n"
          "TRUTH. Zero of them, with a high 'matches' count, means the corpus is internally\n"
          "consistent and a poor score is not coming from here.")
    return 0 if not bad_arith and rt["DRIFTED"] == 0 and not missing_img else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--doc-type", default="receipt")
    ap.add_argument("--doc", default=None, help="doc_id to trace")
    ap.add_argument("--run", default=None, help="also show this run's prediction and verdict")
    ap.add_argument("--audit", type=int, nargs="?", const=0, default=None,
                    help="corpus checks; optional N to limit")
    ap.add_argument("--split", default=None)
    ap.add_argument("--root", default=None, help="override the configured dataset root")
    a = ap.parse_args(argv)

    spec = doctypes.get(a.doc_type)
    entry = dataset_for(spec.name, a.dataset)
    root = a.root or load_cfg()["paths"][entry.root_config_key]

    if a.audit is not None:
        return audit(entry, spec, root, a.split, a.audit)
    if not a.doc:
        sys.exit("pass --doc <doc_id> or --audit")
    trace(entry, spec, a.doc, root, a.run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
