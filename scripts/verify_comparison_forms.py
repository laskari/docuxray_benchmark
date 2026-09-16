#!/usr/bin/env python3
"""Prove the review workbook's "normalised" columns agree with the scorer.

`core.matching.comparison_forms()` renders the two values `compare()` equates, so a reviewer
can see WHY a field was counted correct. It is a second implementation of the same
normalisation, so it can drift. This replays every scored field in a run and requires

        (prediction_form == truth_form)  ==  compare(prediction, truth, rule).exact

for every row. Two rules cannot satisfy that identity by construction and are reported
separately rather than checked:

    text_contained   the verdict is token RECALL of the GT inside the prediction, not equality
    text / address   only under match_policy=threshold, where the verdict is ANLS >= bar
                     (under the published `exact` policy they DO hold, and are checked)

    python scripts/verify_comparison_forms.py runs/s42_main2000

Exit 0 when every checked row agrees, 1 on the first class of disagreement, with examples.
"""
import argparse, collections, json, pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import read_jsonl                                                # noqa: E402
from core.matching import compare, comparison_forms, load_rule_registry, MatchRule   # noqa: E402
from core.metrics import flatten_prediction, unwrap, predicted_value                 # noqa: E402
from core.normalize import _Absent                                                   # noqa: E402
import doctypes                                                                      # noqa: E402
from registry import gt_dir as _gt_dir                                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--examples", type=int, default=5)
    a = ap.parse_args()

    run = pathlib.Path(a.run_dir)
    manifest = json.loads((run / "manifest.json").read_text())
    dataset = a.dataset or manifest.get("dataset")

    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    gt_dir = _gt_dir(spec.name, dataset)
    gt = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)

    checked = agree = 0
    skipped = collections.Counter()
    markers = collections.Counter()
    bad = collections.defaultdict(list)

    for f in sorted((run / "raw").glob("*.json")):
        d = json.loads(f.read_text())
        rec = gt.get(d["doc_id"])
        if rec is None:
            continue
        arms = {k: v for k, v in (d.get("arms") or {}).items()
                if not k.startswith("_") and isinstance(v, dict)}
        flats = {arm: flatten_prediction(unwrap(data, spec)) for arm, data in arms.items()}
        for path in sorted(rec.annotated_fields):
            rule = rules.get(path)
            if rule is None:
                continue
            truth = rec.gt.get(path)
            if isinstance(truth, _Absent):
                continue                       # annotated-absent rows have no GT form to render
            for arm, flat in flats.items():
                pred = predicted_value(path, flat, spec)
                if rule is MatchRule.TEXT_CONTAINED:
                    skipped["text_contained (verdict is token recall)"] += 1
                    continue
                res = compare(pred, truth, rule)
                pf, tf = comparison_forms(pred, truth, rule)
                if pf.startswith("<") and pf.endswith(">"):
                    # A bracketed marker is not a value, so equality is meaningless -- but such
                    # a row must never be scored correct. Check that instead of skipping it.
                    checked += 1
                    if not res.exact:
                        agree += 1
                        markers[pf] += 1
                    elif len(bad[rule.value]) < a.examples:
                        bad[rule.value].append((d["doc_id"], path, arm, pf, tf, res.exact))
                    continue
                checked += 1
                if (pf == tf) == bool(res.exact):
                    agree += 1
                else:
                    key = rule.value
                    if len(bad[key]) < a.examples:
                        bad[key].append((d["doc_id"], path, arm, pf, tf, res.exact))

    print(f"rows checked          {checked}")
    print(f"rows in agreement     {agree}")
    for reason, n in skipped.most_common():
        print(f"not checked           {n}  — {reason}")
    for marker, n in sorted(markers.items()):
        print(f"marker rows           {n}  — rendered {marker}, and all scored not-exact")
    if agree == checked:
        print("\nOK — every rendered normalised form agrees with the scorer's own verdict.")
        return 0
    print(f"\nFAIL — {checked - agree} disagreement(s):")
    for rule, rows in bad.items():
        print(f"\n  rule {rule}")
        for doc, path, arm, pf, tf, ex in rows:
            print(f"    {doc} {path} [{arm}] exact={ex}")
            print(f"      pred form  {pf!r}")
            print(f"      truth form {tf!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
