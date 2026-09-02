"""Guard against the harness silently running a different library stack than the product."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
from check_pins import check   # noqa: E402


def test_requirements_match_ai_backend():
    problems = check()
    assert not problems, "\n".join(problems)
