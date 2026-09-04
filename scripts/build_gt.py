#!/usr/bin/env python3
"""Build ground truth for one (doc type, dataset) pair, and emit its sampling plans.

    python3 scripts/build_gt.py --doc-type invoice --dataset docile100
    python3 scripts/build_gt.py --doc-type invoice --dataset fatura --pilot-size 99

Dataset-agnostic: everything specific lives in the adapter and its reviewed map, so adding a
dataset never edits this file. What it does, in order:

  1. Runs the adapter's check_contract against the reviewed map. A map edited without a
     matching code change stops the build here rather than silently changing ground truth.
  2. Builds every record, validating each one.
  3. Verifies every image referenced actually exists, BEFORE anything is written — a missing
     image discovered mid-run wastes model calls that have already been paid for.
  4. Writes ground_truth.jsonl, coverage.json and coverage.md.
  5. Emits a pilot plan and a full plan.

The coverage report is the honest half. It states, per path, how many documents support it,
how many distinct ground-truth values it has, and whether it clears the headline bar — so a
field measured on nine documents can never quietly appear beside one measured on ninety.
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

import doctypes                                                    # noqa: E402
from core.canonical import write_jsonl                             # noqa: E402
from core.normalize import _Absent                                 # noqa: E402
from registry import dataset_for, gt_dir as _gt_dir                # noqa: E402

# A rate published as a headline needs enough support to mean anything, and enough VARIETY
# that it is not one value repeated. Both bars are stated here rather than left to the reader.
MIN_DOCS_FOR_HEADLINE = 30
MIN_DISTINCT_FOR_HEADLINE = 20


def _distinct(values) -> int:
    out = set()
    for v in values:
        if v is None or isinstance(v, _Absent):
            continue
        out.add(json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else str(v))
    return len(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc-type", default="invoice")
    ap.add_argument("--dataset", default=None,
                    help="required when the doc type has more than one dataset")
    ap.add_argument("--pilot-size", type=int, default=10,
                    help="documents in the pilot plan (the hand-adjudication set)")
    ap.add_argument("--seed", type=int, default=20260903)
    ap.add_argument("--skip-image-check", action="store_true",
                    help="build ground truth without verifying every image is present")
    a = ap.parse_args(argv)

    spec = doctypes.get(a.doc_type)
    entry = dataset_for(spec.name, a.dataset)
    adapter = entry.load()
    out_dir = _gt_dir(spec.name, entry.name)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"dataset : {entry.name}  ({adapter.root})")
    print(f"doc type: {spec.name}")
    print(f"map     : {entry.map_path}")

    # ---- 1. the contract ----------------------------------------------------
    adapter.check_contract(str(_ROOT / entry.map_path))
    print("contract: code and reviewed map agree")

    # ---- 2. records ---------------------------------------------------------
    records = list(adapter.iter_records())
    if not records:
        print("!! the adapter produced no records")
        return 2
    print(f"records : {len(records)} built and validated")

    # ---- 3. images ----------------------------------------------------------
    if not a.skip_image_check:
        missing = [r.doc_id for r in records if not (adapter.root / r.image_path).exists()]
        if missing:
            print(f"!! {len(missing)} of {len(records)} images are missing under "
                  f"{adapter.root}")
            print(f"   first missing: {adapter.root / records[0].image_path}")
            print("   Ground truth NOT written. Fix the dataset root or the adapter's "
                  "image_path before spending anything on a run.")
            return 2
        print(f"images  : all {len(records)} present")

    # ---- 4. coverage --------------------------------------------------------
    by_path_docs = collections.Counter()
    by_path_clusters = collections.defaultdict(set)
    by_path_values = collections.defaultdict(list)
    absent_docs = collections.Counter()
    excluded = collections.Counter()
    n_rows = 0
    for r in records:
        for p in r.annotated_fields:
            by_path_docs[p] += 1
            by_path_clusters[p].add(r.cluster_id)
            v = r.gt.get(p)
            by_path_values[p].append(v)
            if isinstance(v, _Absent):
                absent_docs[p] += 1
        for reason in r.excluded_fields.values():
            excluded[reason[:80]] += 1
        n_rows += len(r.gt.get("lineItems") or [])

    paths = []
    for p in sorted(by_path_docs):
        distinct = _distinct(by_path_values[p])
        n_docs = by_path_docs[p]
        # A path constant across every cluster measures the dataset, not the model — the
        # FATURA seller block passed a distinct-value count and failed this one.
        constant = distinct <= 1 and n_docs > 1
        paths.append({
            "path": p,
            "n_docs": n_docs,
            "n_clusters": len(by_path_clusters[p]),
            "distinct_gt_values": distinct,
            "absent_docs": absent_docs[p],
            "headline_eligible": bool(n_docs >= MIN_DOCS_FOR_HEADLINE
                                      and distinct >= MIN_DISTINCT_FOR_HEADLINE
                                      and not constant),
            "constant_across_clusters": constant,
        })

    clusters = {r.cluster_id for r in records}
    keysets = collections.Counter(r.keyset_id for r in records)
    coverage = {
        "dataset": entry.name, "doc_type": spec.name,
        "n_docs": len(records), "n_clusters": len(clusters), "n_keysets": len(keysets),
        "n_gt_line_item_rows": n_rows,
        "scoreable_paths": len(paths),
        "headline_eligible_paths": sum(1 for p in paths if p["headline_eligible"]),
        "headline_bar": {"min_docs": MIN_DOCS_FOR_HEADLINE,
                         "min_distinct_values": MIN_DISTINCT_FOR_HEADLINE},
        "exclusions": dict(excluded.most_common()),
        "paths": paths,
    }
    (out_dir / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")

    md = [f"# {entry.name} — ground-truth coverage", "",
          f"{len(records)} documents · {len(clusters)} clusters · {len(keysets)} key-sets "
          f"· {n_rows} annotated line-item rows", "",
          f"{len(paths)} scoreable paths, of which "
          f"{coverage['headline_eligible_paths']} clear the headline bar "
          f"(>= {MIN_DOCS_FOR_HEADLINE} documents and >= {MIN_DISTINCT_FOR_HEADLINE} distinct "
          f"ground-truth values, and not constant across clusters).", "",
          "| path | docs | clusters | distinct | absent | headline |",
          "|---|---|---|---|---|---|"]
    for p in paths:
        md.append(f"| `{p['path']}` | {p['n_docs']} | {p['n_clusters']} | "
                  f"{p['distinct_gt_values']} | {p['absent_docs']} | "
                  f"{'yes' if p['headline_eligible'] else 'no'} |")
    if excluded:
        md += ["", "## Exclusions recorded, with reasons", ""]
        for reason, count in excluded.most_common():
            md.append(f"* **{count}** — {reason}")
    (out_dir / "coverage.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ---- 5. ground truth + plans -------------------------------------------
    n = write_jsonl(records, str(out_dir / "ground_truth.jsonl"))
    print(f"written : {out_dir / 'ground_truth.jsonl'} ({n} records)")

    by_cluster = collections.defaultdict(list)
    for r in records:
        by_cluster[r.cluster_id].append(r.doc_id)
    natural = len(by_cluster) == len(records)     # one document per cluster: a natural corpus

    rng = random.Random(a.seed)
    index = {r.doc_id: r for r in records}

    # The pilot must exercise every schema variant AND the full range of table sizes. Sampling
    # at random would, on this corpus, very likely draw ten single-row tables and tell us
    # nothing about the case that actually breaks — a 44-row table.
    by_keyset = collections.defaultdict(list)
    for r in records:
        by_keyset[r.keyset_id].append(r.doc_id)
    pilot = [sorted(v)[len(v) // 2] for v in by_keyset.values()]

    strata = collections.defaultdict(list)
    for r in records:
        rows = int(r.meta.get("n_gt_rows") or 0)
        strata["none" if rows == 0 else
               "single" if rows == 1 else
               "small" if rows <= 3 else
               "medium" if rows <= 9 else "large"].append(r.doc_id)
    for name in ("none", "single", "small", "medium", "large"):
        for doc in rng.sample(sorted(strata[name]), min(2, len(strata[name]))):
            if len(pilot) >= a.pilot_size:
                break
            if doc not in pilot:
                pilot.append(doc)
    pilot = sorted(pilot)

    full = sorted(r.doc_id for r in records) if natural else sorted(
        d for c in sorted(by_cluster) for d in rng.sample(sorted(by_cluster[c]),
                                                          min(20, len(by_cluster[c]))))

    plans = [
        (f"pilot_{len(pilot)}", pilot, {
            "purpose": "verification gate and the hand-adjudication set — not a measurement",
            "selection": "one document per distinct key-set, then filled by line-item table "
                         "size (none / single / small / medium / large) so the pilot cannot "
                         "consist of ten easy single-row tables",
            "strata": {k: len(v) for k, v in strata.items()}}),
        (f"main_{len(full)}", full, {
            "purpose": "the reported numbers",
            "selection": ("every document — the corpus is small enough that sampling would "
                          "only add variance" if natural
                          else f"20 per cluster, seed {a.seed}"),
            "seed": a.seed}),
    ]
    for name, ids, meta in plans:
        (out_dir / f"{name}.json").write_text(json.dumps({
            "dataset": entry.name, "doc_type": spec.name,
            "n_docs": len(ids),
            "n_templates": len({index[d].cluster_id for d in ids}),
            "n_gt_rows": sum(int(index[d].meta.get("n_gt_rows") or 0) for d in ids),
            **meta, "doc_ids": ids}, indent=2), encoding="utf-8")
        print(f"plan    : {name}.json  {len(ids):>4d} docs / "
              f"{sum(int(index[d].meta.get('n_gt_rows') or 0) for d in ids):>5d} GT rows")

    print(f"\ncoverage: {coverage['scoreable_paths']} scoreable paths, "
          f"{coverage['headline_eligible_paths']} headline-eligible — see coverage.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
