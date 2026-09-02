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
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms A,B,C,D --limit 10 \
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
| 5 | run extraction + judge across arms A/B/C | [05](docs/05_RUNNING.md) |
| 6 | side-by-side sheet for human adjudication | [06](docs/06_COMPARISON.md) |
| 7 | metrics — overall, per field, per file | [07](docs/07_METRICS.md) |

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
scripts/    environment checks, cost estimation and reconstruction
```

**The rule that keeps it portable:** if the answer differs between an invoice and a receipt it
belongs in `doctypes/`; if it is the same for both it belongs in `core/`.

## Status

* Field map **v1.0-frozen** — 29 rows, all accepted (Naveen Kumar, Sandeep)
* **154 tests passing**
* Invoice ground truth built for 10,000 FATURA documents; 26 scoreable paths, 21 headline-eligible
* Cost measured at **$0.0905/document** for arms A+B+C (~$118 for the full programme)
* Receipt support: `DocTypeSpec` in place, awaiting a dataset adapter

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
