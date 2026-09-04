# Runbook — running the benchmark on your Mac

The model calls must run on your machine. Claude's sandbox reaches `pypi.org` but its egress
proxy returns **403 at CONNECT for `generativelanguage.googleapis.com`**, so the Gemini API is
unreachable from there. Nothing was spent proving this — the probe run cost $0.0000.

Because `runs/` is inside the connected folder, whatever you produce here Claude can read and
score without any file transfer. You execute, Claude scores.

## One-time

```bash
cd ~/Documents/Xelp_work/DocuXray/DocuXray_prod/docuxray_benchmark
PYTHON=python3.11 ./setup.sh          # 3.11 matches ai_backend's Dockerfile and has a PyMuPDF wheel
source ~/.venvs/docuxray-benchmark-Darwin-arm64/bin/activate
source activate.env                    # sets PYTHONPATH to ../docuxray_ai_backend
cp .env.example .env                   # then paste GEMINI_API_KEY into it
python scripts/check_env.py            # must print READY (shows the key masked, never in full)
python -m pytest tests/ -q             # 154 tests
```

## The ladder — stop at each gate

Every step writes `runs/<run_id>/manifest.json`, `raw/*.json` (one record per document, the
scoring input) and `stages/<doc_id>/*.json` (each stage the moment it returns — raw extraction,
the judge's input, the judge report, refined, shipped). Scoring adds `results.{json,md}`.
The response cache means a re-score after a metric fix costs nothing.

```bash
# 1. COST PROBE — replaces the thinking-token estimate with a measurement.  ~$0.58
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL --limit 10 \
    --run-id costprobe --concurrency 2
python scripts/recost.py runs/costprobe                    # reproject from the real numbers

# 2. SMOKE — 51 docs, one per template + Template1_Instance0.              ~$2.97
#    Verification, NOT a measurement. 26/26 scoreable paths, 12/12 policy branches.
python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms RAW,FINAL --run-id smoke
python steps/step7_metrics.py runs/smoke

# 3. PILOT — 99 docs, one per distinct key-set.                            ~$5.76
#    Then hand-adjudicate EVERY mismatch: model error / GT error / normalisation / mapping.
python steps/step5_run.py --plan gt/invoice/pilot_99.json --arms RAW,FINAL --run-id pilot
python steps/step6_compare.py runs/pilot                   # the adjudication sheet
python steps/step7_metrics.py runs/pilot

# 4. VARIANCE — the same 50 docs three times, to establish the noise floor. ~$8.73
#    RAW only: the judge is skipped entirely, so this measures extraction noise and costs
#    only the extraction calls.
for i in 1 2 3; do python steps/step5_run.py --plan gt/invoice/pilot_99.json --arms RAW \
    --limit 50 --run-id "variance-$i"; done

# 5. MAIN — 1,000 docs, 50 templates x 20. The reported numbers.           ~$58.20
python steps/step5_run.py --plan gt/invoice/main_1000.json --arms RAW,FINAL --run-id main
python steps/step7_metrics.py runs/main
```

**Any run can be re-issued with `--resume`.** It carries forward every document that already
has a complete `raw/*.json` and pays only for the rest, so a stalled or aborted run is picked
up rather than repeated.

## Safety rails already in the runner

- **Spend cap** `$250` (`config.yaml`). Exceeding it aborts and writes a partial manifest.
- **Early abort**: after 10 documents the runner projects the full cost and aborts if it would
  breach the cap — so a wrong estimate costs you ten documents, not a thousand.
- **`job_id=None` throughout**, which is what keeps Redis and MongoDB out of the path.
- **Nothing in the product is modified.** All three repos are clean; the harness imports them
  read-only.
- **Per-stage wall-clock budgets** — extract 300s, judge 420s, free stages 60s
  (`run.stage_timeouts`). A stage that overruns fails that one document, keeps every stage it
  already completed on disk, and lets the queue carry on. Nothing in the harness can freeze.

## If a run looks stuck

It will not any more, but if a document reports `TMOUT`:

```bash
jq -r '[.doc_id,(.stage_status|tostring),.error]|@tsv' runs/<id>/stages/*/00_status.json
```

names the stage that overran. `totals` and `invoiceInfo` judge sections are the ones seen
stalling. **Lower `concurrency` before raising a timeout** — at 2 a document is 4 extraction
calls then 5 judge sections (10 in flight); at 4 it is 20, which is where the stalls appeared.
Then `--resume` with `--judge-timeout 600`. Full account in `docs/05_RUNNING.md`.

## What to look at first in `results.md`

1. `hallucinations` — emitted where GT says the field is absent. The damaging class.
2. **`harm rate`** — fields correct in RAW that FINAL got wrong (judge + refinement + postprocessing together; the gap does not attribute to the judge alone). A positive net lift with a
   high harm rate is not a good trade, and this is the number that decides whether the judge
   earns its place.
3. `no-tax slice` — all-docs vs taxed-only recall. 45% of scoreable TOTAL values sit on pages
   with no tax label, where the including/excluding distinction cannot be got wrong.
4. `95% CI` — from a bootstrap resampling **templates**. If it is wide, that is the honest
   answer for a 50-layout corpus, not a bug.
