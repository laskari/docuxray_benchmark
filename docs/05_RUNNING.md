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
