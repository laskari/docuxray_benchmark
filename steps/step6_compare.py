#!/usr/bin/env python3
"""STEP 6 — side-by-side comparison for human review.

Excel export: one row per (document, field) with ground truth beside every arm, the harness's
verdict for each, what changed between arms, what the judge said, and a blank adjudication
column. This is the artifact the pilot's hand review is done in.

    python steps/step6_compare.py runs/smoke
"""
import pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.review_export import main                              # noqa: E402
raise SystemExit(main())
