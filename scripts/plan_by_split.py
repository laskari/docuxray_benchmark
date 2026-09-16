#!/usr/bin/env python3
"""Emit a run plan restricted to one dataset split, stratified by table size.

    python3 scripts/plan_by_split.py --doc-type receipt --dataset cord --split test  --name main
    python3 scripts/plan_by_split.py --doc-type receipt --dataset cord --split dev --size 20 --name pilot
    python3 scripts/plan_by_split.py --doc-type receipt --dataset cord --split dev --size 10 --name smoke

WHY THIS EXISTS RATHER THAN A FLAG ON build_gt.py.

scripts/build_gt.py emits three plans on a rule that is right for a clustered corpus and
degenerate for a natural one: smoke is one document per cluster, and pilot is one per key-set
plus filler. On CORD-v2 every document is its own cluster and there are 287 distinct key-sets,
so those come out as smoke=1000 and pilot=287 -- a "verification gate" that costs as much as the
measurement, and a hand-adjudication set nobody can hand-adjudicate. The generic rule is not
wrong; it just has no answer for a corpus with no repeating unit, and quietly changing it would
alter FATURA's plans too.

WHY SPLIT-AWARE. CORD-v2 ships train/dev/test and the adapter builds all three into one corpus
tagged with meta['split']. Piloting the field map on dev and reporting on test is the whole
reason for preferring v2 over the flattened train-only copy: every map decision is then made on
documents the reported number is not computed from.

STRATIFIED BY TABLE SIZE, not at random. 431 of 1,000 CORD receipts have a single line item, so
a uniform sample of 20 draws about 9 single-row tables and tells you nothing about the case that
actually breaks row alignment -- the 22-row table, or the receipt with two items sharing a name.
The strata are the same ones build_gt.py uses, so the two are comparable.
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

import doctypes                                          # noqa: E402
from core.canonical import read_jsonl                    # noqa: E402
from registry import dataset_for, gt_dir as _gt_dir      # noqa: E402


def stratum(n_rows: int) -> str:
    return ("none" if n_rows == 0 else "single" if n_rows == 1 else
            "small" if n_rows <= 3 else "medium" if n_rows <= 9 else "large")


ORDER = ("none", "single", "small", "medium", "large")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc-type", default="receipt")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--split", required=True, help="value of meta['split'] to keep")
    ap.add_argument("--size", type=int, default=0, help="0 = every document in the split")
    ap.add_argument("--name", required=True, help="plan name; the file is <dataset>_<split>_<name>_<n>.json")
    ap.add_argument("--seed", type=int, default=20260907)
    ap.add_argument("--exclude-plan", default=None,
                    help="path to a plan whose doc_ids must NOT appear (keeps a pilot and the "
                         "reported set disjoint)")
    a = ap.parse_args(argv)

    spec = doctypes.get(a.doc_type)
    entry = dataset_for(spec.name, a.dataset)
    gt_dir = _gt_dir(spec.name, entry.name)
    src = gt_dir / "ground_truth.jsonl"
    if not src.exists():
        print(f"!! no ground truth at {src}. Run scripts/build_gt.py first.")
        return 2

    records = [r for r in read_jsonl(str(src)) if (r.meta or {}).get("split") == a.split]
    if not records:
        seen = sorted({(r.meta or {}).get("split") for r in read_jsonl(str(src))})
        print(f"!! no records with split {a.split!r}. Splits present: {seen}")
        return 2

    banned = set()
    if a.exclude_plan:
        banned = set(json.loads(pathlib.Path(a.exclude_plan).read_text())["doc_ids"])
        records = [r for r in records if r.doc_id not in banned]

    strata = collections.defaultdict(list)
    for r in records:
        strata[stratum(int((r.meta or {}).get("n_gt_rows") or 0))].append(r.doc_id)

    if a.size and a.size < len(records):
        rng = random.Random(a.seed)
        # Round-robin across strata so the rarest table sizes are represented before the
        # commonest one fills the plan. Within a stratum the draw is seeded and reproducible.
        pools = {k: rng.sample(sorted(v), len(v)) for k, v in strata.items()}
        chosen: list = []
        while len(chosen) < a.size and any(pools.values()):
            for k in ORDER:
                if len(chosen) >= a.size:
                    break
                if pools.get(k):
                    chosen.append(pools[k].pop())
        ids = sorted(chosen)
        selection = (f"{a.size} of {len(records)} {a.split} documents, round-robin across "
                     f"line-item table size (none/single/small/medium/large), seed {a.seed}")
    else:
        ids = sorted(r.doc_id for r in records)
        selection = (f"every document in the {a.split} split — no sampling, so there is no "
                     f"sampling variance to argue about")

    index = {r.doc_id: r for r in records}
    got = collections.Counter(stratum(int((index[d].meta or {}).get("n_gt_rows") or 0))
                              for d in ids)
    out = gt_dir / f"{entry.name}_{a.split}_{a.name}_{len(ids)}.json"
    out.write_text(json.dumps({
        "dataset": entry.name, "doc_type": spec.name, "split": a.split,
        "n_docs": len(ids),
        "n_templates": len({index[d].cluster_id for d in ids}),
        "n_gt_rows": sum(int((index[d].meta or {}).get("n_gt_rows") or 0) for d in ids),
        "purpose": {"main": "the reported numbers",
                    "pilot": "hand-adjudicate every mismatch; the map may be amended from this "
                             "set and from no other",
                    "smoke": "wiring gate, not a measurement"}.get(a.name, a.name),
        "selection": selection,
        "seed": a.seed,
        "strata": dict(got),
        "excluded_from": a.exclude_plan or None,
        "doc_ids": ids,
    }, indent=2), encoding="utf-8")

    print(f"  {out.name:34s} {len(ids):>4d} docs / "
          f"{sum(int((index[d].meta or {}).get('n_gt_rows') or 0) for d in ids):>5d} GT rows"
          f"   strata={dict(got)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
