#!/usr/bin/env python3
"""STEP 7 — metrics: overall, per field, per file.

    python steps/step7_metrics.py runs/smoke
    python steps/step7_metrics.py runs/docile_main --dataset docile100

Writes results.json and results.md into the run directory. Confidence intervals come from a
bootstrap resampling CLUSTERS (templates), not documents.
"""
import argparse, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.metrics import score_run                               # noqa: E402
from registry import REGISTRY                                    # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_dir", help="the run directory to score, e.g. runs/docile_main")
ap.add_argument("--doc-type", default=None)
ap.add_argument("--dataset", default=None, choices=sorted(REGISTRY),
                help="which ground truth to score against; needed only when the run's "
                     "manifest does not record one and the doc type has several datasets")
a = ap.parse_args()
score_run(pathlib.Path(a.run_dir), a.doc_type, a.dataset)
print((pathlib.Path(a.run_dir) / "results.md").read_text(encoding="utf-8"))
