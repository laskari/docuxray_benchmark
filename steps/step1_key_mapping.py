#!/usr/bin/env python3
"""STEP 1 — key mapping: dataset labels -> DocuXray schema paths.

Produces the inputs a human needs to author the map, and verifies the map once written.

    python steps/step1_key_mapping.py --doc-type invoice --dataset-labels    # what the data has
    python steps/step1_key_mapping.py --doc-type invoice --schema-paths      # what the schema offers
    python steps/step1_key_mapping.py --doc-type invoice --check             # code vs reviewed map

The map is authored BLIND -- from label semantics against the schema's own field descriptions,
before looking at any model output. See docs/01_KEY_MAPPING.md for why that matters.
"""
from __future__ import annotations
import argparse, collections, json, pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import doctypes                                                    # noqa: E402
from core.matching import load_rule_registry                       # noqa: E402
from registry import dataset_for                                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-type", default="invoice")
    ap.add_argument("--dataset", default=None, help="dataset key; default = the doc type's first")
    ap.add_argument("--dataset-labels", action="store_true")
    ap.add_argument("--schema-paths", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    spec = doctypes.get(a.doc_type)
    entry = dataset_for(spec.name, a.dataset)

    if a.schema_paths:
        tsv = _ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"
        if not tsv.exists():
            print(f"!! {tsv} not built. Run:\n"
                  f"   PYTHONPATH=<ai_backend> python core/schema_paths.py {spec.schema_model} "
                  f"> {tsv}")
            return 2
        rules = load_rule_registry(str(tsv), spec)
        print(f"{len(rules)} scoreable targets for {spec.schema_model}\n")
        for path, rule in sorted(rules.items()):
            print(f"  {path:56s} {rule.value}")

    if a.dataset_labels:
        adapter = entry.load()
        counts, samples = collections.Counter(), collections.defaultdict(list)
        for doc_id, raw in adapter.iter_raw():
            for label, value in entry.iter_labels(raw):
                counts[label] += 1
                if len(samples[label]) < 3 and value is not None:
                    samples[label].append(str(value)[:60])
        print(f"{len(counts)} distinct labels in {entry.name}\n")
        for label, n in counts.most_common():
            print(f"  {label:32s} {n:>6d}  e.g. {samples[label]}")

    if a.check:
        adapter = entry.load()
        adapter.check_contract(str(_ROOT / entry.map_path))
        print(f"contract OK — datasets/{entry.module} agrees with {entry.map_path}")
    if not (a.schema_paths or a.dataset_labels or a.check):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
