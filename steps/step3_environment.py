#!/usr/bin/env python3
"""STEP 3 — is this machine ready to run the pipeline?

Verifies the API key (masked, never printed), the model ids, every path, and that all four
production entry points import with no Redis, MongoDB or S3 running. Exits non-zero if not,
so it can gate a run.

    python steps/step3_environment.py
"""
import pathlib, subprocess, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
raise SystemExit(subprocess.call([sys.executable, str(_ROOT / "scripts" / "check_env.py")]))
