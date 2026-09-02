# DocuXray extraction benchmark — overview

Measures the **shipped** extraction pipeline against a public dataset's ground truth, one
document type at a time. Nothing in `docuxary_backend`, `docuxray_ai_backend` or any prompt is
modified: the harness imports the production modules read-only and calls them directly, with no
FastAPI, no RQ, no Redis, no MongoDB, no S3.

## The seven steps

| step | what it settles | entry point |
|---|---|---|
| 1 | key mapping — dataset labels → schema paths | `steps/step1_key_mapping.py` |
| 2 | ground truth in canonical form | `steps/step2_ground_truth.py` |
| 3 | is this machine able to run the pipeline | `steps/step3_environment.py` |
| 4 | which documents to run, and why | `steps/step4_sampling.py` |
| 5 | run extraction + judge | `steps/step5_run.py` |
| 6 | side-by-side comparison for human review | `steps/step6_compare.py` |
| 7 | metrics — overall, per field, per file | `steps/step7_metrics.py` |

Steps 1, 2 and 4 cost nothing. Step 5 is the only one that spends money.

## Layout

```
core/        doc-type agnostic. Nothing here knows what an invoice is.
  normalize.py      money, dates, strings, phones, addresses, currency, ANLS
  matching.py       type-driven match rules
  canonical.py      BenchmarkRecord — the one interchange type
  schema_paths.py   generates scoreable paths from the production Pydantic models
  runner.py         arms A/B/C, response cache, spend cap, manifest
  metrics.py        three-state nulls, cluster bootstrap, judge confusion matrix
  review_export.py  the Excel comparison sheet

doctypes/    everything specific to invoice vs receipt vs bank statement, in ONE file
datasets/    one adapter per (dataset x doc type); base.py is the contract
mapping/     reviewed field maps and known ground-truth defects, per dataset
steps/       the seven entry points above
schema/      generated path inventories (do not hand-edit)
gt/<type>/   built ground truth and sampling plans
runs/<id>/   manifest, raw outputs, results, review sheet
```

## The rule that keeps it portable

**If the answer differs between an invoice and a receipt, it belongs in `doctypes/`.
If it is the same for both, it belongs in `core/`.**

Every `core/` function takes an optional `spec` (a `DocTypeSpec`). It falls back to invoice for
convenience, never because `core/` assumes a document type.

## Adding a document type

See `NEW_DOCTYPE_CHECKLIST.md`. Short version: one `DocTypeSpec`, one dataset adapter, one
reviewed field map, one registry entry. No change to `core/`.

## Non-negotiables, and why

* **The measured system is frozen.** Improving the product to score better is not measurement.
* **Three-state nulls.** Present / annotated-absent / not-annotated are different things.
  Collapsing the last two inflates every number, because most schema fields are null on most
  documents.
* **Confidence intervals resample clusters, not documents.** 200 renderings of one template are
  one observation.
* **The field map is authored blind and frozen.** Ground truth defined by model output measures
  nothing.
* **No number ships without a manifest** naming the model, prompt version and all three git SHAs.
