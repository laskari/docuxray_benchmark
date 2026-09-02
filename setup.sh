#!/usr/bin/env bash
# Create the benchmark's Python environment.
#
#   ./setup.sh                    # uses whatever python3 is on PATH
#   PYTHON=python3.11 ./setup.sh  # recommended: matches ai_backend's Dockerfile
#
# The venv is created OUTSIDE the repo, at ~/.venvs/docuxray-benchmark-<os>-<arch>. This folder
# is shared between the macOS checkout and the Linux sandbox, so a single in-repo .venv would
# hold binaries for the wrong platform half the time.
#
# ai_backend has no venv to reuse -- it runs in Docker (scripts/setup_dev.sh, Python 3.11).
# requirements.txt mirrors ai_backend/requirements.txt exactly; scripts/check_pins.py enforces it.

set -uo pipefail                       # NOT -e: a failed install is handled, not fatal
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV="${BENCHMARK_VENV:-$HOME/.venvs/docuxray-benchmark-$(uname -s)-$(uname -m)}"
PY="${PYTHON:-python3}"

command -v "$PY" >/dev/null || { echo "!! '$PY' not found on PATH"; exit 1; }
PYVER=$($PY -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo ">>> python: $($PY -V)  ($PY)"
$PY -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
  echo "!! need Python 3.10 or newer"; exit 1; }
[ "$PYVER" = "3.11" ] || echo "    note: ai_backend's Dockerfile pins 3.11; PYTHON=python3.11 ./setup.sh is the closest match"

echo ">>> venv:   $VENV"
[ -d "$VENV" ] || "$PY" -m venv "$VENV" || { echo "!! could not create the venv"; exit 1; }
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip --quiet

# Try the exact pins first. If ANY package has no wheel for this interpreter, pip installs
# nothing at all -- which is how an empty venv happens. So on failure, fall back to the overlay
# that floats PyMuPDF (safe: fitz is imported but never called on the benchmark's path) and
# say so plainly. Detecting by failure beats guessing by version number.
echo ">>> installing exact pins (requirements.txt)"
if python -m pip install -r requirements.txt --quiet 2>/tmp/dxb_pip.log; then
  REQ=requirements.txt
else
  echo
  echo "    exact pins could not be installed on Python $PYVER. Last lines:"
  tail -4 /tmp/dxb_pip.log | sed 's/^/      /'
  echo
  echo "    falling back to requirements-nowheel.txt, which floats PyMuPDF ONLY."
  echo "    That is safe: ai/extraction/pdf_utils.py imports fitz at module load, but it is"
  echo "    only CALLED from the mime_type=='application/pdf' branch, which this benchmark"
  echo "    never enters (JPG input). Every other pin still matches ai_backend exactly."
  echo "    Cleanest fix if you want the exact pin:  PYTHON=python3.11 ./setup.sh"
  echo
  if python -m pip install -r requirements-nowheel.txt 2>&1 | tail -5; then
    REQ=requirements-nowheel.txt
  else
    echo "!! both requirement sets failed to install. Full log: /tmp/dxb_pip.log"; exit 1
  fi
fi

echo ">>> verifying"
python - <<'PYCHECK'
import importlib, sys
missing = [m for m in ("yaml", "pydantic", "PIL", "fitz", "google.genai",
                       "structlog", "redis", "rq", "pymongo", "pytest", "openpyxl")
           if not importlib.util.find_spec(m.split(".")[0])]
if missing:
    print("   !! still missing:", ", ".join(missing)); sys.exit(1)
print("   all imports present")
PYCHECK
[ $? -eq 0 ] || { echo "!! verification failed — the venv is incomplete"; exit 1; }

cat <<MSG

    installed from: $REQ

    activate with:

        source $VENV/bin/activate
        source activate.env

    then:

        python scripts/check_env.py
        python -m pytest tests/ -q

MSG
