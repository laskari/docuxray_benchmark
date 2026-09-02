#!/usr/bin/env python3
"""STEP 5 — run the pipeline over a plan.

Thin wrapper over core/runner.py so the seven steps read as one sequence.

    python steps/step5_run.py --plan gt/invoice/smoke_51.json --arms A,B,C --run-id smoke
    python steps/step5_run.py --plan gt/invoice/smoke_51.json --limit 10 --run-id costprobe
"""
import pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.runner import main                                     # noqa: E402
raise SystemExit(main())
