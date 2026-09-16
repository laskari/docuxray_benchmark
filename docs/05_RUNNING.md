# Step 5 — running extraction and the judge

**Goal:** capture what the pipeline produces at each stage. **The only step that spends money.**

```bash
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL --run-id smoke
python steps/step5_run.py --plan gt/invoice/smoke_51.json --limit 10 --run-id costprobe
```

## The two arms

Verified against the worker enqueue chain, not assumed from the module names:

```
extraction_worker ── run_extraction_postprocessing_pipeline ──> judge_queue
judge_worker ──> refinement_queue
refinement_worker ──> postprocessing_queue
postprocessing_worker   (LAST, on refined_data, first_pass=True)
```

| arm | stages | model calls | what it is in production |
|---|---|---|---|
| **RAW** | extract + `extraction_postprocessing` | 4 (split parts) | `pages.$.extraction_postprocessing_result` — **exactly what the judge is fed** |
| **FINAL** | RAW + judge + refinement + `<type>_postprocessor(first_pass=True)` | 5 (judge sections); the rest deterministic | `pages.$.postprocessing_result` — **the shipped output** |

Both come out of **one pass** — 9 calls per document, not 18.

**Why only two.** Each arm is chosen because it is a state the product itself persists and a
human can point at: the judge's input, and what the customer receives. The two states in
between — bare extraction, and refined-before-postprocessing — are written to `stages/` for
inspection but are **not scored**, because scoring them adds two columns that invite comparisons
between states that never both exist in a production record. Nothing is lost: extraction and
the judge report are cached, refinement and postprocessing are deterministic, so
`scripts/derive_arms.py` rebuilds either intermediate into a scoreable run at zero cost.

**What RAW → FINAL does and does not tell you.** The gap spans judge **+** refinement **+**
postprocessing together. It answers *"does everything after extraction help, end to end?"* —
a real product question, and the one the landing page will quote. It cannot attribute a
change to the judge specifically rather than to the final postprocessor. The judge's
**detector matrix** (`07_METRICS.md`) is judge-specific, because RAW is precisely its input;
`fields_fixed` / `fields_harmed` are not. Say so wherever those numbers appear.

**The type postprocessor runs after refinement, not before it.** `judge_worker.py:88` feeds the
judge `extraction_postprocessing_result or extraction_result` — never the postprocessor's
output. Verified offline: `normalizedValue` first appears in FINAL, so the judge only ever sees
`{"originalValue": "18.37"}`.

### A finding this ordering retracted

An earlier version of this harness ran the type postprocessor before the judge, so the judge was fed
normalised numbers it never sees in production. It duly flagged them as format errors, and the
benchmark reported "the judge reverts the postprocessor's normalisation, `discountTotal`
100% → 33%" as a production defect. **It was an artifact of the harness.**

Getting the stage order wrong does not produce a slightly-off number. It produces a confident,
wrong story about the product. Before trusting any arm comparison, trace the enqueue chain.

## What each run writes

```
runs/<id>/manifest.json                              the run
          raw/<doc_id>.json                          one record per document -- step6/step7 read this
          stages/<doc_id>/00_status.json             progress, rewritten after every stage
                          01_extract_raw.json        intermediate, straight from Gemini
                          02_extraction_postprocessing.json   >> arm RAW -- the judge's input
                          03_judge_report.json       the judge's verdict, per section
                          04_refined.json            intermediate, before postprocessing
                          05_postprocessed.json      >> arm FINAL -- the shipped output
```

`raw/` is the scoring input and is written once, when a document finishes. `stages/` is written
**the moment each stage returns**, which is what makes a partial document useful: a run that
dies in the judge still leaves four paid-for extraction calls on disk, and

```bash
cat runs/<id>/stages/*/00_status.json | python3 -c 'import json,sys;[print(json.loads(l)) for l in sys.stdin]'
jq -r '[.doc_id,(.stage_status|tostring)]|@tsv' runs/<id>/stages/*/00_status.json
```

tells you where a long run actually is, instead of guessing from the last console line. Each
stage file carries its own `stage_ms`, `cached` flag, model id, and — for the judge — the
`judge_input_sha` that proves which data the verdict was computed from.

Budget roughly **75 KB per document** for `stages/`; the 1,000-document main run is about 75 MB.

## Two ways to reach Gemini: `sync` and `batch`

`run.model_mode` in `config.yaml`, or `--mode` per invocation. It changes **how the bytes are
fetched, not what is asked or what is measured.**

```bash
# the default: interactive calls, `concurrency` documents at a time
python steps/step5_run.py --plan gt/receipt/sroie/main_626.json --run-id main

# the Gemini Batch API: one job for the whole plan, 50% of the price
python steps/step5_run.py --plan gt/receipt/sroie/main_626.json --run-id main --mode batch
python steps/step5_run.py --plan ... --mode batch --batch-stages extract   # extraction only
```

| | sync | batch |
|---|---|---|
| extraction | 4–5 interactive calls per document | (4–5 × N) requests in one job |
| judge | 5 interactive calls per document | (≤5 × N) requests in one job |
| price | list | **50%** |
| latency | N ÷ concurrency round trips | one wait for everything (~24h SLO, usually far less) |
| retries | `_run_parts` retries a part 3× | one shot, then a synchronous retry here |
| `concurrency` | governs the run | irrelevant — nothing is in flight |

**How it works.** Batch mode is a **pre-pass**, not a different runner. Before `run_plan`
starts, `core/batch.py` submits every model call the plan will make, waits, and writes each
answer into the *same response cache* `_extract` and `_verify` already read. `run_plan` then
walks the documents exactly as it always has and finds every model stage already answered. So
arms, `--resume`, `runs/<id>/stages/`, scoring and every test are untouched, and

> **a batch run and a sync run differ only in how the bytes were fetched.**

`tests/test_batch.py` pins that claim directly: given the same model output, a document that
came through the batch produces byte-identical `RAW` and `RAW_POSTPROCESSED` to one that came
through an interactive call.

**The cache key is deliberately identical for both modes.** Same model, same prompt version,
same schema, same prepared bytes means the same measurement — so an existing cache stays valid
and switching modes never re-pays. Provenance is not lost: the cached metadata carries a
`batch` block naming the jobs and the discount, the stage status reads `batch` rather than
`cache`, and the manifest records `model_mode` plus per-stage counts.

### What batch is not

**Not the path production runs.** Production extracts interactively, per document. Two
differences are real, and are recorded rather than papered over:

* **Retries differ.** Sync extraction retries a part up to 3× inside `_run_parts` on truncated
  JSON or schema failure; a batch request gets one shot. A part that comes back broken is
  retried **here, synchronously and at list price**, through the product's own
  `extract_from_buffer`, so it lands back on the retried path instead of being scored as a
  null. The per-document cost then reports the mixture (`cost.sync_retry_cost`), not a flat
  halving.
* **No think-token stream.** `_stream_part` / `_stream_section` exist to publish to Redis, which
  this harness already disabled with `job_id=None`, so nothing is lost — but the batch response
  is one blob, not a stream.

**Anything the batch cannot complete falls through to the synchronous path.** A document whose
extraction is still incomplete after the sync retry is simply *not cached*, and `run_plan`
extracts it interactively like any other cache miss. A partial extraction in the cache would be
read back as a complete one by this run and every run after it, so it is never written.

The judge takes the coarser fallback: **any** failed section drops the whole document, which is
then re-judged synchronously. Aggregate confidence is the **minimum** over sections, so a report
missing a section is not slightly incomplete — it is a different, more confident report.

### Stage order

`--batch-stages` defaults to `extract,judge` and always runs in that order. The judge's cache
key hashes *the data it is asked to verify*, so it does not exist until extraction has landed.
That is the pipeline, not an implementation detail: `judge_worker.py:88` is fed
`extraction_postprocessing_result`.

### If it is interrupted

**The tokens are committed at submit time, not at collect time.** The job name is written to
`runs/<id>/batch/<stage>.json` *before* the first poll, and re-running the same command picks
that job up instead of submitting — and paying for — the plan a second time. This happens with
or without `--resume`, which governs which *documents* run, not which jobs are collected.

`--batch-job-timeout` (default 86400s) only stops *waiting*; it does not cancel the job.

### The spend cap works differently here

`run_plan`'s cap check runs per document, *after* each is paid for. One `create_batch` commits
the whole plan at once, so the check has to happen **before** submission: the pre-pass projects
from this cache's own measured cost per document and refuses to submit rather than overrun. On
the first run of a dataset there is nothing to project from and it says so — run a small
`--limit` first if the cap matters.

## Safety rails

* **Spend cap** (`config.yaml`) — exceeding it aborts and writes a partial manifest.
* **Early abort** — after N documents the runner projects the full cost and stops if it would
  breach the cap. A wrong estimate costs ten documents, not a thousand.
* **Response cache** keyed on `sha256(image) + model + prompt_version + arm`. Re-scoring after a
  metric fix costs nothing, and re-running a plan only pays for what is new.
* **Per-stage wall-clock budgets** (`run.stage_timeouts` in `config.yaml`) — see below.
* **Up-front image check** — one clear failure naming the dataset root, rather than N identical
  `FileNotFoundError`s.

## Measure the cost, do not estimate it

Run 10 documents first, then `python scripts/recost.py runs/costprobe`.

My FATURA estimate was **55% low** because thinking tokens turned out to be **91% of billed
output**. Measured: $0.0905/doc against an estimated $0.0582.

Watch for the two shapes: extraction reports usage as flat `prompt_tokens`/`thinking_tokens`,
the judge nests it under `token_usage`, and the cost total is keyed `total_cost`. Getting that
wrong reports $0.00 with tokens counted.

## Manifest

Every run writes `runs/<id>/manifest.json`: run id, UTC timestamp, plan, arms, model ids,
git SHA of all three repos (with `+dirty`), field-map version, tokens, cost, cache hits.
**No number ships without one.**

## When a run appears to freeze

It happened on 2026-09-02: ten documents, three of them stuck, no output, no exit.

Three documents stalled inside **one judge section each** while their other four came back in
seconds — `Template10` and `Template16` on `totals`, `Template14` on `invoiceInfo`. Nothing ever
timed out, because:

* `ai/llm/gemini_provider.py:83` builds `genai.Client(api_key=key)` with **no `http_options`**,
  so there is no HTTP read timeout, and the judge reads via `generate_content_stream` — a
  stalled stream never returns;
* `ai/judge/core/judge.py:705` waits on its five sections with `as_completed(futures)` and **no
  timeout**, so one stuck section hangs that whole document, permanently;
* the judge has **no retry path at all** (extraction has `max_retries = 3` with backoff), so a
  stalled section is terminal rather than slow;
* and `run_plan` then waited on `as_completed(pending)` with no timeout, inside a
  `with ThreadPoolExecutor(...)` whose `__exit__` joins hung threads — so the process would not
  exit even after being asked to.

It was run at `concurrency: 4`, which fires 4 × 5 = **20 judge calls at once**.

None of the product's four causes can be fixed from the harness — it imports `ai_backend`
read-only. What the harness does instead is **stop waiting**:

* every stage runs on a **daemon** thread with its own budget (`_run_with_timeout`). Daemon
  matters: `concurrent.futures` installs an `atexit` hook that joins its workers, so an
  abandoned executor thread is what kept the interpreter alive;
* a stage that overruns is recorded as `timeout` in `stage_status`, the document is failed, and
  **the queue keeps moving**;
* stages already completed stay on disk, so `--resume` re-runs only the stalled document and
  pays only for what is missing;
* the global deadline is now only a backstop. If it fires, that is a harness bug, not a slow
  model;
* abandoned threads are listed in the manifest as `leaked_threads`. Their tokens are spent but
  unaccounted, so a run with leaks costs slightly more than it reports.

```bash
# defaults: extract 300s, judge 420s (a legitimate totals section has been measured at 136s)
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL \
    --limit 10 --run-id probe --concurrency 2
# then, for whatever stalled -- costs only the missing documents
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL \
    --limit 10 --run-id probe --resume --judge-timeout 600
```

**Lower `concurrency` before raising a timeout.** At concurrency 2 a document is 4 extraction
calls then 5 judge sections, so 10 calls in flight; at 4 it is 20, which is where the stalls
were observed.
