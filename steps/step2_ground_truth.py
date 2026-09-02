#!/usr/bin/env python3
"""STEP 4 — which documents to run, and why.

Step 2 emits the plans as a side effect of building ground truth. This step re-emits them
WITHOUT rebuilding (useful after changing a sample size), and explains what each plan covers.

    python steps/step4_sampling.py --doc-type invoice --describe
    python steps/step4_sampling.py --doc-type invoice --instances-per-cluster 20 --seed 20260901

Three plans, three purposes:

  smoke     one document per cluster. A VERIFICATION gate, not a measurement -- does the
            runner reach every stage, does scoring produce sane numbers, does the cache work.
  pilot     one document per distinct KEY-SET, so every schema variant appears. This is the
            hand-adjudication set: every mismatch is classified by a human.
  main      N documents per cluster, seeded. The reported numbers.

The sampling trap this exists to avoid: taking the FIRST document of each cluster. On FATURA
that is Instance0, and FATURA seeded each template's fixed seller block from its own Instance0
buyer -- so 27 of 31 Instance0 documents carry a duplicate-address quirk that appears in ZERO
of the other 6,169. Sampling the first of anything is a bet that position carries no meaning.
"""
from __future__ import annotations
import argparse, collections, json, pathlib, random, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import doctypes                                                   # noqa: E402
from core.canonical import read_jsonl                             # noqa: E402
from registry import dataset_for                                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-type", default="invoice")
    ap.add_argument("--instances-per-cluster", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--describe", action="store_true")
    a = ap.parse_args()

    spec = doctypes.get(a.doc_type)
    gt_dir = _ROOT / "gt" / spec.name
    src = gt_dir / "ground_truth.jsonl"
    if not src.exists():
        print(f"!! no ground truth at {src}. Run step 2 first.")
        return 2
    records = read_jsonl(str(src))

    if a.describe:
        for f in sorted(gt_dir.glob("*.json")):
            if f.name == "coverage.json":
                continue
            d = json.loads(f.read_text())
            print(f"\n{f.name}")
            print(f"  {d['n_docs']} documents · {d['n_templates']} clusters")
            print(f"  purpose   : {d.get('purpose','')[:150]}")
            print(f"  selection : {d.get('selection','')[:150]}")
            if d.get("branch_coverage"):
                print(f"  covers    : {d['branch_coverage'][:150]}")
            if d.get("not_covered"):
                print(f"  NOT       : {d['not_covered'][:150]}")
        return 0

    by_cluster = collections.defaultdict(list)
    by_keyset = collections.defaultdict(list)
    for r in records:
        by_cluster[r.cluster_id].append(r.doc_id)
        by_keyset[r.keyset_id].append(r.doc_id)

    def mid(ids):
        s = sorted(ids)
        return s[len(s) // 2]

    smoke = sorted(sorted(v)[1] if len(v) > 1 else sorted(v)[0] for v in by_cluster.values())
    entry = dataset_for(spec.name)
    known = {r.doc_id for r in records}
    smoke.extend(d for d in entry.extra_smoke_docs if d in known and d not in smoke)
    entry = dataset_for(spec.name)
    known = {r.doc_id for r in records}
    smoke.extend(d for d in entry.extra_smoke_docs if d in known and d not in smoke)
    pilot = sorted(mid(v) for v in by_keyset.values())
    rng = random.Random(a.seed)
    main_plan = sorted(d for c in sorted(by_cluster)
                       for d in rng.sample(sorted(by_cluster[c]),
                                           min(a.instances_per_cluster, len(by_cluster[c]))))

    index = {r.doc_id: r for r in records}
    for name, ids, meta in (
        ("smoke", smoke, {"purpose": "verification gate, not a measurement",
                          "selection": "one document per cluster (not the first — see the header)"}),
        ("pilot", pilot, {"purpose": "hand-adjudicate every mismatch",
                          "selection": "one document per distinct key-set (median instance)"}),
        (f"main_{len(main_plan)}", main_plan,
         {"purpose": "the reported numbers",
          "selection": f"{a.instances_per_cluster} per cluster, seed {a.seed}", "seed": a.seed}),
    ):
        path = gt_dir / f"{name if name.startswith('main') else name + '_' + str(len(ids))}.json"
        path.write_text(json.dumps({
            "n_docs": len(ids),
            "n_templates": len({index[d].cluster_id for d in ids}),
            "templates": sorted({index[d].cluster_id for d in ids}),
            **meta, "doc_ids": ids}, indent=2), encoding="utf-8")
        print(f"  {path.name:22s} {len(ids):>5d} docs / "
              f"{len({index[d].cluster_id for d in ids}):>3d} clusters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
