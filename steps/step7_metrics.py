#!/usr/bin/env python3
"""STEP 7 — metrics: overall, per field, per file.

    python steps/step7_metrics.py runs/smoke

Writes results.json and results.md into the run directory. Confidence intervals come from a
bootstrap resampling CLUSTERS (templates), not documents.
"""
import argparse, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.metrics import score_run                               # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("run_dir")
ap.add_argument("--doc-type", default=None)
a = ap.parse_args()
score_run(pathlib.Path(a.run_dir), a.doc_type)
print((pathlib.Path(a.run_dir) / "results.md").read_text(encoding="utf-8"))
