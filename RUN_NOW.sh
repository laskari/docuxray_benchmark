#!/usr/bin/env bash
# Three gates, in order. Stop and look at the output of each before running the next.
#
#   ./RUN_NOW.sh probe     10 documents, 10 templates   ~$0.90   wiring + cost check
#   ./RUN_NOW.sh smoke     51 documents, 50 templates   ~$3.70   per-key performance
#   ./RUN_NOW.sh main    1000 documents, 50 x 20        ~$90     the reported numbers
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
  main)
    python steps/step5_run.py --plan gt/invoice/main_1000.json \
        --arms RAW,FINAL --run-id main --concurrency 2
    python steps/step7_metrics.py runs/main
    python steps/step6_compare.py runs/main
    ;;
  *)
    echo "usage: ./RUN_NOW.sh {probe|smoke|main}"; exit 2 ;;
esac
echo
echo "Done. Add --resume to the step5 line to pick up a run that stalled or was interrupted."
