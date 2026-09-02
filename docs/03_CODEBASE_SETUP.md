# Step 3 — codebase and environment

**Goal:** this machine can call extraction and the judge, with the key never entering a
transcript. **Cost:** nothing.

```bash
PYTHON=python3.11 ./setup.sh          # 3.11 matches ai_backend's Dockerfile
source ~/.venvs/docuxray-benchmark-$(uname -s)-$(uname -m)/bin/activate
source activate.env                    # PYTHONPATH -> ../docuxray_ai_backend
cp .env.example .env                   # paste GEMINI_API_KEY into it
python steps/step3_environment.py      # must print READY
```

## Calling the production pipeline offline

All three stages run with **no infrastructure** — verified, not assumed:

```python
from ai.extraction.gemini_extraction_service import GeminiExtractionService
from ai.judge.core.judge import DocumentJudge
from ai.refinement.pipeline import run_refinement_pipeline

svc.extract(image_path, part="all", document_id=doc_id, job_id=None)
DocumentJudge().verify_sectional(..., job_id=None, page_index=None)
run_refinement_pipeline(data, judge_report, doc_type)     # deterministic, no model call
```

**`job_id=None` is what makes this work.** Every Redis and MongoDB call site in extraction and
the judge is guarded by `if job_id:`; both connection getters are lazy. The packages must
import, but no service need be running.

**Do not call `ai.judge.pipeline.run_judge_pipeline()`** — its signature forces a `job_id`
through to `verify_sectional`, re-enabling the Mongo and Redis paths. Call the judge directly.

## Pins

`requirements.txt` mirrors `ai_backend/requirements.txt` exactly for all nine shared packages,
and `scripts/check_pins.py` enforces it **as a test**. If the harness ran a different `pydantic`
than the product, it would be benchmarking a different stack, quietly.

`PIN_EXCEPTIONS` allows drift only where a package provably never executes on the benchmark's
path — currently PyMuPDF, which is imported by `pdf_utils.py` but only called on the
`mime_type == "application/pdf"` branch. Nothing joins that list without that argument.

## Two traps

**`pip install -r` is all-or-nothing.** One package without a wheel for your interpreter and pip
installs *nothing*, leaving a venv that exists but is empty. `setup.sh` now detects the failure,
falls back, and verifies all eleven imports before declaring success.

**Venvs live outside the repo**, at `~/.venvs/docuxray-benchmark-<os>-<arch>`. The folder is
shared between a macOS checkout and a Linux sandbox; one in-repo `.venv` would hold binaries for
the wrong platform half the time.

## The key

`.env` is gitignored and never read into a transcript. `config.py::mask()` renders it as
`<39 chars, ends …K_qU>` — enough to confirm it loaded and catch a truncated paste.

Egress matters: a sandbox may reach `pypi.org` but not `generativelanguage.googleapis.com`.
Run step 5 where the API is actually reachable.
