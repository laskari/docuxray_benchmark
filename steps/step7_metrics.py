#!/usr/bin/env python3
"""STEP 7 — metrics: overall, per field, per file.

    python steps/step7_metrics.py runs/smoke
    python steps/step7_metrics.py runs/docile_main --dataset docile100

Three arms are scored when the run carries them: RAW (what the judge is shown),
RAW_POSTPROCESSED (a free scoring construct that puts RAW through the product's number
parser) and FINAL (shipped). The judge's fixed/harmed are measured against
RAW_POSTPROCESSED; its detector matrix stays on RAW. See config.yaml's `scoring:` block.
Scoring refuses to run when the arms do not cover the same documents.

Writes results.json and results.md into the run directory. Confidence intervals come from a
bootstrap resampling CLUSTERS (templates), not documents.
"""
import argparse, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.metrics import (ArmDenominatorMismatch, NumericSourceMismatch,  # noqa: E402
                          score_run)
from registry import REGISTRY                                    # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_dir", help="the run directory to score, e.g. runs/docile_main")
ap.add_argument("--doc-type", default=None)
ap.add_argument("--dataset", default=None, choices=sorted(REGISTRY),
                help="which ground truth to score against; needed only when the run's "
                     "manifest does not record one and the doc type has several datasets")
ap.add_argument("--numeric-source", default=None,
                choices=("normalizedValue", "originalValue"),
                help="which key of a NumericValue to score. normalizedValue answers 'is the "
                     "VALUE right?'; originalValue answers 'is the TRANSCRIPTION right?' and is "
                     "required by a verbatim dataset. Default: config.yaml scoring.numeric_source.")
ap.add_argument("--match-policy", default=None, choices=("exact", "threshold"),
                help="exact (default) counts a value only if it is identical after "
                     "normalisation; threshold lets text and address rules pass at a similarity "
                     "bar (--anls-threshold).")
ap.add_argument("--anls-threshold", type=float, default=None,
                help="the similarity bar the threshold policy uses for text and address rules. "
                     "Default 0.8, the ICDAR/DocVQA convention. Ignored under exact.")
ap.add_argument("--out-suffix", default=None,
                help="write results<suffix>.{json,md} instead of results.{json,md}. Defaults to "
                     "_<dataset> whenever --dataset names a ground truth other than the one the "
                     "manifest records, so a second scoring can never overwrite the first.")
ap.add_argument("--allow-numeric-source-mismatch", action="store_true",
                help="score a verbatim dataset against normalizedValue anyway. The result "
                     "misreports formatting as reading error and is not publishable.")
ap.add_argument("--allow-arm-mismatch", action="store_true",
                help="score even when the arms cover different document sets. The resulting "
                     "arm-to-arm comparisons are between different corpora and are not "
                     "publishable; back-fill the missing arm instead "
                     "(scripts/add_raw_pp_arm.py).")
a = ap.parse_args()
run = pathlib.Path(a.run_dir)

# The suffix is derived, not asked for: scoring the same run against a second ground truth and
# letting it land on results.md would make the published numbers whichever scoring ran last.
suffix = a.out_suffix
if suffix is None:
    import json
    manifest_dataset = json.loads((run / "manifest.json").read_text(encoding="utf-8")).get("dataset")
    suffix = f"_{a.dataset}" if (a.dataset and a.dataset != manifest_dataset) else ""
    if a.match_policy and a.match_policy != "exact":
        bar = a.anls_threshold if a.anls_threshold is not None else 0.8
        suffix += f"_{a.match_policy}{str(bar).replace('.', '')}"

# A verbatim dataset declares the only numeric key its stored values can be compared against;
# honour it unless the caller has explicitly overridden --numeric-source.
scoring = {k: v for k, v in (("numeric_source", a.numeric_source),
                            ("match_policy", a.match_policy),
                            ("anls_threshold", a.anls_threshold)) if v is not None} or None
if a.numeric_source is None and a.dataset:
    from registry import REGISTRY
    required = REGISTRY[a.dataset].requires_numeric_source
    if required:
        scoring = dict(scoring or {}, numeric_source=required)
        print(f"-- dataset {a.dataset} requires numerics on {required}; using it.")

try:
    score_run(run, a.doc_type, a.dataset, scoring=scoring,
              allow_arm_mismatch=a.allow_arm_mismatch,
              out_suffix=suffix,
              allow_numeric_source_mismatch=a.allow_numeric_source_mismatch)
except (ArmDenominatorMismatch, NumericSourceMismatch) as exc:
    # Not a crash: a refusal. Nothing was written, so no half-scored results.md is left
    # behind to be read as a result.
    print(f"!! refusing to score {a.run_dir}\n   {exc}")
    raise SystemExit(2)
print((run / f"results{suffix}.md").read_text(encoding="utf-8"))
