# DocuXray extraction benchmark

Measures the **shipped** extraction pipeline against a public dataset's ground truth, one
document type at a time. Nothing in `docuxary_backend`, `docuxray_ai_backend` or any prompt is
modified — the harness imports the production modules read-only.

**Start with [`docs/00_OVERVIEW.md`](docs/00_OVERVIEW.md).**
To benchmark a new document type, follow [`docs/NEW_DOCTYPE_CHECKLIST.md`](docs/NEW_DOCTYPE_CHECKLIST.md).

## Quick start

```bash
PYTHON=python3.11 ./setup.sh
source ~/.venvs/docuxray-benchmark-$(uname -s)-$(uname -m)/bin/activate && source activate.env
cp .env.example .env                                  # paste GEMINI_API_KEY

python steps/step1_key_mapping.py --doc-type invoice --check
python steps/step2_ground_truth.py --doc-type invoice
python steps/step3_environment.py
python steps/step4_sampling.py --doc-type invoice --describe
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL --limit 10 \
    --run-id probe --concurrency 2       # --resume picks up anything that timed out
python scripts/recost.py runs/probe                   # measure cost before committing
python steps/step6_compare.py runs/probe              # Excel review sheet
python steps/step7_metrics.py runs/probe              # results.json + results.md
```

Steps 1–4 cost nothing. Step 5 is the only one that spends money.

## The seven steps

| # | settles | doc |
|---|---|---|
| 1 | dataset labels → schema paths, authored **blind**, frozen | [01](docs/01_KEY_MAPPING.md) |
| 2 | ground truth in canonical, portable form | [02](docs/02_GROUND_TRUTH.md) |
| 3 | this machine can call the pipeline, key never in a transcript | [03](docs/03_CODEBASE_SETUP.md) |
| 4 | which documents to run, and why | [04](docs/04_SAMPLING.md) |
| 5 | run extraction + judge across arms RAW/FINAL | [05](docs/05_RUNNING.md) |
| 6 | side-by-side sheet for human adjudication | [06](docs/06_COMPARISON.md) |
| 7 | metrics — overall, per field, per file | [07](docs/07_METRICS.md) |

## Results

| report | corpus | held out? |
|---|---|---|
| [11 — SROIE](docs/11_SROIE_RESULTS.md) | 337 receipts, original ICDAR `test` split | **yes** |
| [09 — CORD-v2](docs/09_CORD_RESULTS.md) | 100 receipts, `test` split, with line items | yes |
| [10 — invoices + receipts](docs/10_BENCHMARK_REPORT.md) | FATURA 999 + CORD 100 | no (FATURA) |

Every figure in these carries the run directory it came from, and every run directory is
re-scoreable for free — the response cache is keyed on image bytes, so no report costs money
to reproduce.

## Layout

```
core/       doc-type agnostic. Nothing here knows what an invoice is.
doctypes/   everything specific to invoice vs receipt, in ONE file
datasets/   one adapter per (dataset x doc type); base.py is the contract,
            _template.py is the starting point for a new one
mapping/    reviewed field maps + known ground-truth defects
steps/      the seven entry points
schema/     generated path inventories (do not hand-edit)
gt/<type>/  built ground truth and sampling plans
runs/<id>/  manifest, raw outputs, results, review sheet, and stages/<doc_id>/ with
            every intermediate written the moment it returns: 01_extract_raw,
            02_extraction_postprocessing (the judge's input), 03_judge_report,
            04_refined, 05_postprocessed, plus 00_status for live progress
scripts/    environment checks, cost estimation, dataset prep and reconstruction
            archive/ holds one-off investigations kept for provenance (see its README)
```

**The rule that keeps it portable:** if the answer differs between an invoice and a receipt it
belongs in `doctypes/`; if it is the same for both it belongs in `core/`.

## Status

* Invoice field map **v1.0-frozen** — 29 rows, all accepted (Naveen Kumar, Sandeep)
* Receipt field map **1.1-draft** — contracts 1.12–1.19 await two reviewers' sign-off
* **464 tests**; baseline is 2 known failures on `$` currency handling (in-progress work)
* Invoice ground truth built for 10,000 FATURA documents; 26 scoreable paths, 21 headline-eligible
* Receipt datasets: SROIE (958, original ICDAR train/test split) and CORD-v2 (1,000)
* Cost measured at **$0.0905/document** for both arms in one pass

## Current state of the invoice benchmark

Done: mapping frozen · ground truth built · plans emitted · runner and scorer verified ·
cost probe run (10 docs, 100% success).

Next: smoke (51 docs) → pilot (99, hand-adjudicated) → variance probe → main run (1,000).

Fix logged 2026-09-02: a 10-document run hung with no output — three documents stalled inside a
single judge section each (`totals`, `totals`, `invoiceInfo`) while their other four returned in
seconds. `ai/llm` sets no HTTP read timeout and `judge.py:705` waits on its sections with no
timeout, so a stalled stream hangs that document permanently, and the judge has no retry path.
None of that is fixable from a read-only harness, so the harness now runs every stage on a daemon
thread under its own budget, writes each stage to `runs/<id>/stages/` the moment it returns, and
fails one document instead of the run. See `docs/05_RUNNING.md`.

Correction logged 2026-09-01: an earlier arm definition ran the type postprocessor before the
judge, producing a "judge reverts normalisation" finding that was an artifact of the harness.
Arms now match the verified production order — extraction → extraction_postprocessing → judge →
refinement → postprocessing — and there are four of them. The costprobe judge outputs were
computed from the wrong input and have been invalidated; re-run the probe before reading any
judge metric. See `docs/05_RUNNING.md`.

Correction logged 2026-09-03: the harness reproduced two production stages instead of calling
them, and had drifted from both. `ai.pipeline_core` is now the single definition and both the
worker and this harness call it.

* **The tail was short one step.** `_postprocess` stopped at `processor.process()`;
  `postprocessing_worker` then ran `validate_format` + `apply_format_nulls` on top. That step
  only ever nulls leaves, so arm FINAL could score a value the product ships as null — the
  benchmark flattering the system it measures. Re-checked against the two probe documents that
  exposed it: neither has a format violation, so no number already published moves.
* **The extractor was fed the wrong bytes.** The app never extracts from the source file. It
  extracts from the quality stage's clean image, and on the modal path those bytes are a
  `convert("RGB")` + default-quality JPEG re-encode of the source
  (`ai/quality/rotation.py:170`) served under a `.jpg` key. The harness was sending the
  original PNG. Same model, different pixels — and the field differences that produced looked
  like model nondeterminism. Now controlled by `run.image_prep` and part of the cache key, so
  every existing cached extraction and verdict is correctly invalidated.

Correction logged 2026-09-16: **`ai/pipeline_core.py` had gone missing from the ai_backend
checkout** — only stale `__pycache__` bytecode remained, and the file was never tracked in git,
so `import ai.pipeline_core` failed and `_extract`, `_postprocess` and `_image_prep_version`
were all broken. It has been **reconstructed from `ai/__pycache__/pipeline_core.cpython-314.pyc`
and verified bytecode-identical to it**: same code objects, same constants, same instructions,
same line numbers. Only comments and blank-line placement are new, because those leave no trace
in bytecode — everything executable is exact, and `tests/test_pipeline_parity.py` passes against
the real postprocessors. It is committed as ai_backend `3be4376`, but only on the local
`report_bug_fixes` branch — **not on `main`**, so any other ai_backend checkout lacks it and
every step fails at import. It must reach `main` before this harness can run from a clean clone.

Added 2026-09-16: **batch mode** (`run.model_mode: batch`, or `--mode batch`). Extraction and
the judge can go through the Gemini Batch API — one job for the whole plan at 50% of the
interactive price — instead of one interactive call per part and per section. It is implemented
as a pre-pass that fills the response cache the runner already reads, so the runner, the arms
and the scoring are untouched and a batch run differs from a sync run only in how the bytes
were fetched. The cache key is deliberately the same for both modes. See `docs/05_RUNNING.md`.

`scripts/rebuild_final.py` re-derives a completed run's FINAL arm from its stored
`04_refined.json`, with no model calls, for when the postprocessing tail changes. Run against
`runs/probe` after the format-contract fix: **0 of 51 documents changed**, so that run's
published numbers stand and did not need re-paying for. Anything upstream of the tail — the
prompt, the model, `run.image_prep` — moves 01/03/04 and does need a real run.

`scripts/compare_with_app.py` diffs a run's FINAL arm against a JSON copied out of the app and
attributes each differing leaf to the stage that caused it — serve-layer, formatting,
extraction, judge+refinement, or tail. Use it instead of comparing the two files by eye: on the
Template10/Template11 pair, eyeballing suggests four broken sections and the attributed answer
is two serve-layer keys, eight extraction-level differences, five judge rewrites and two
knock-ons from those rewrites, with nothing wrong in the harness.

Product findings from that investigation, neither of them harness bugs:

* The judge is **degrading correct extractions**, and not only on the two documents that
  started the investigation. Measured across all 51 documents of `runs/probe`, from the stage
  dumps already on disk, no model calls:

  | | of 51 |
  |---|---|
  | judge raised ≥1 issue | 38 |
  | refinement changed a value | 38 |
  | refinement wrapped a percentage in parentheses | **18** |
  | `totals.taxPercentage.normalizedValue` destroyed by that wrap | **16** |

  24 paren wraps in total: 16 on `taxPercentage.originalValue`, 8 on
  `discountPercentage.originalValue`. `taxPercentage.originalValue` was rewritten on 17 of 51
  documents. Judge issue types: 43 `format_error`, 24 `missing_value`,
  13 `character_value_mismatch`.

  The mechanism is a contract collision, not a misreading. The FATURA artwork really does print
  `(3.88%)`, so the judge is right about the glyphs and raises `format_error`; refinement makes
  `originalValue` match the page; and `_common.py` then applies the accounting convention where
  surrounding parentheses mean negative — correct for `(102.68)` as money, wrong for a printed
  percentage — so `(4.65%)` parses to −4.65, fails the `[0, 100]` check and is nulled as
  `percent_out_of_range`. Net effect: **`taxPercentage` is null on roughly a third of invoices
  that state a tax rate**, from a value extraction had read correctly.

  Two places to fix it, and they are not equivalent: stop treating parentheses as negation for
  percentage fields in `_normalize_numeric_value` (small, targeted, keeps the accounting rule
  for money), or stop refinement rewriting `originalValue` into a form that breaks its own
  field's numeric contract (larger, and addresses the class rather than this instance).
  Untouched here — it is a product decision, and it moves the thing the benchmark measures.
* `ai/quality/rotation.py:170` returns `_image_to_bytes(img)` — the **un-rotated** original,
  not `horizontalized` — so the app computes a rotation correction, discards it, and pays a
  JPEG quality-75 re-encode for nothing. Left alone deliberately: changing it would move the
  input the benchmark is measuring. Fix it on its own merits, then bump `IMAGE_PREP_VERSION`.
