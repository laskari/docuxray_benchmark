#!/usr/bin/env bash
# Three gates, in order. Stop and look at the output of each before running the next.
#
#   ./RUN_NOW.sh probe     10 documents, 10 templates   ~$0.90   wiring + cost check
#   ./RUN_NOW.sh smoke     51 documents, 50 templates   ~$3.70   per-key performance
#   ./RUN_NOW.sh main-a   500 documents, 50 x 10       ~$43     first half of the main run
#   ./RUN_NOW.sh main-b   500 documents, 50 x 10       ~$43     second half, run it later
#   ./RUN_NOW.sh merge   1000 documents                  FREE    recombine -> the headline
#
# Gemini is not reachable from the Claude sandbox, so this runs in your own Terminal.
# The response cache carries forward: the probe's 10 documents are free inside the smoke.
set -euo pipefail
cd "$(dirname "$0")"
source "${BENCHMARK_VENV:-$HOME/.venvs/docuxray-benchmark-$(uname -s)-$(uname -m)}/bin/activate"

case "${1:-}" in
  probe)
    python steps/step5_run.py --plan gt/invoice/smoke_51.json --limit 10 \
        --arms RAW,FINAL --run-id probe --concurrency 2
    python scripts/recost.py runs/probe
    python steps/step7_metrics.py runs/probe
    python steps/step6_compare.py runs/probe
    ;;
  smoke)
    python steps/step5_run.py --plan gt/invoice/smoke_51.json \
        --arms RAW,FINAL --run-id smoke --concurrency 2
    python steps/step7_metrics.py runs/smoke
    python steps/step6_compare.py runs/smoke
    ;;
  main-a)
    python steps/step5_run.py --plan gt/invoice/fatura/main_500a.json \
        --arms RAW,FINAL --run-id main_a --concurrency 2
    python scripts/recost.py runs/main_a
    python steps/step7_metrics.py runs/main_a --dataset fatura
    python steps/step6_compare.py runs/main_a --dataset fatura
    ;;
  main-b)
    python steps/step5_run.py --plan gt/invoice/fatura/main_500b.json \
        --arms RAW,FINAL --run-id main_b --concurrency 2 --resume
    python scripts/recost.py runs/main_b
    python steps/step7_metrics.py runs/main_b --dataset fatura
    python steps/step6_compare.py runs/main_b --dataset fatura
    ;;
  merge)
    # Free. Recombines the two halves into the real 1,000-document measurement.
    # Confidence intervals from 1,000 are narrower than from either half, so the headline
    # must come from here -- never from averaging the two halves' numbers.
    python scripts/merge_runs.py runs/main_a runs/main_b --out runs/main
    python steps/step7_metrics.py runs/main --dataset fatura
    python steps/step6_compare.py runs/main --dataset fatura
    ;;
  *)
    echo "usage: ./RUN_NOW.sh {probe|smoke|main-a|main-b|merge}"; exit 2 ;;
esac
echo
echo "Done. Add --resume to the step5 line to pick up a run that stalled or was interrupted."
