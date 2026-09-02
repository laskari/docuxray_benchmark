#!/usr/bin/env bash
# Which interpreters are available, and which can install the exact pins?
echo "PATH python3 -> $(command -v python3)  $(python3 -V 2>&1)"
for v in 3.11 3.12 3.13; do
  p=$(command -v "python$v" || true)
  [ -n "$p" ] && echo "python$v      -> $p  $($p -V 2>&1)"
done
command -v conda >/dev/null && echo "conda        -> $(conda --version) (base python: $(conda run -n base python -V 2>&1))"
echo
echo "PyMuPDF 1.24.0 has macOS arm64 wheels for cp38-cp312 only."
echo "  3.11 is the best choice: matches ai_backend's Dockerfile AND has a wheel."
