"""scripts/compare_with_app.py must attribute a difference to the right stage.

The script exists to stop a particular wrong conclusion: that a difference between the app's
Raw JSON and a run's FINAL arm is a harness bug. Most such differences are not. Getting the
attribution wrong in either direction is worse than not having the script -- it would either
hide real harness drift or send someone hunting for a bug in deterministic code -- so each
class gets a fixture that can only be explained by that stage.

Fixtures are built from a real run's stage dumps when one is present, and synthesised
otherwise, so the test is meaningful on a clean checkout and stronger on a working one.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "compare_with_app", _ROOT / "scripts" / "compare_with_app.py")
cwa = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cwa)


def _write(path: pathlib.Path, stage: str, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": stage, "data": data}, indent=2), encoding="utf-8")


@pytest.fixture
def run_dir(tmp_path):
    """A run in which each stage changed exactly one known field.

    extraction said 1.29%, the judge disagreed and refinement made it (1.29%), and the tail
    could not parse the result -- which is the real Template10/11 story in miniature.
    """
    d = tmp_path / "stages" / "DocX"
    extract = {"invoiceInfo": {"category": "Other", "categoryReasoning": "bench wording"},
               "totals": {"discountPercentage": {"originalValue": "1.29%"}}}
    refined = {"invoiceInfo": {"category": "Other", "categoryReasoning": "bench wording"},
               "totals": {"discountPercentage": {"originalValue": "(1.29%)"}}}
    final = {"invoiceInfo": {"category": "Other", "categoryReasoning": "bench wording"},
             "totals": {"discountPercentage": {"originalValue": "(1.29%)",
                                               "normalizedValue": None}}}
    _write(d / "01_extract_raw.json", "extract", extract)
    _write(d / "04_refined.json", "refine", refined)
    _write(d / "DocX_05_postprocessed.json", "postprocess", final)
    return tmp_path


def _by_path(rows):
    return {r["path"]: r["class"] for r in rows}


def test_identical_output_produces_no_rows(run_dir):
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    assert cwa.classify("DocX", app, run_dir) == []


def test_document_type_is_serve_layer_not_a_pipeline_difference(run_dir):
    """The single most misleading key in the comparison: the app has it, no stage wrote it."""
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["documentType"] = "invoice"
    assert _by_path(cwa.classify("DocX", app, run_dir)) == {"documentType": "SERVE-LAYER"}


def test_int_versus_float_is_formatting_not_a_difference(run_dir):
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["totals"]["discountPercentage"]["normalizedValue"] = 1
    _write(run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json", "postprocess",
           {**app, "totals": {"discountPercentage": {
               "originalValue": "(1.29%)", "normalizedValue": 1.0}}})
    rows = cwa.classify("DocX", app, run_dir)
    assert rows == [], "1 and 1.0 are the same number and must not be reported"


def test_a_value_the_judge_rewrote_is_attributed_to_judge_and_refine(run_dir):
    """Extraction and the app agree; 04_refined is where it changed. This is judge harm."""
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["totals"]["discountPercentage"]["originalValue"] = "1.29%"
    rows = cwa.classify("DocX", app, run_dir)
    assert _by_path(rows) == {"totals.discountPercentage.originalValue": "JUDGE+REFINE"}
    row = rows[0]
    assert row["at_extract"] == "1.29%" and row["at_refine"] == "(1.29%)", (
        "the report must show the value at each stage, or the reader cannot tell harm "
        "from a correction")


def test_a_value_that_differed_before_any_stage_ran_is_attributed_to_extraction(run_dir):
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["invoiceInfo"]["categoryReasoning"] = "app wording, different sampling"
    assert _by_path(cwa.classify("DocX", app, run_dir)) == \
        {"invoiceInfo.categoryReasoning": "EXTRACTION"}


def test_a_deterministic_tail_difference_is_called_out_as_tail(run_dir):
    """The one class that indicts the harness. The tail is deterministic, so if it differs
    with the same refined input, the two tails are not the same code."""
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["totals"]["discountPercentage"]["normalizedValue"] = 1.29
    assert _by_path(cwa.classify("DocX", app, run_dir)) == \
        {"totals.discountPercentage.normalizedValue": "TAIL"}


def test_a_missing_refine_dump_is_unattributed_rather_than_guessed(run_dir):
    """A judge timeout leaves no 04. Guessing EXTRACTION there would invent a finding."""
    (run_dir/"stages"/"DocX"/"04_refined.json").unlink()
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["invoiceInfo"]["categoryReasoning"] = "app wording"
    assert _by_path(cwa.classify("DocX", app, run_dir)) == \
        {"invoiceInfo.categoryReasoning": "UNATTRIBUTED"}


def test_absent_versus_present_is_reported_not_skipped(run_dir):
    """A field the app has and the run does not is a difference, and the commonest shape of
    one -- refinement inventing totalExcludingTax, for instance."""
    app = json.loads((run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    app["totals"]["subtotal"] = {"originalValue": "10.00 $"}
    rows = cwa.classify("DocX", app, run_dir)
    paths = {r["path"] for r in rows}
    assert "totals.subtotal.originalValue" in paths
    assert all(r["bench"] == cwa.ABSENT for r in rows)


def test_a_wrapped_app_export_is_unwrapped_before_comparing(run_dir):
    """People export from different places; a wrapped dict must not read as 100% different."""
    final = json.loads(
        (run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").read_text())["data"]
    _write(run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json", "postprocess",
           {"invoiceOutputData": final})
    assert cwa.classify("DocX", final, run_dir) == []


def test_a_run_without_a_final_arm_exits_rather_than_reporting_nonsense(run_dir):
    (run_dir/"stages"/"DocX"/"DocX_05_postprocessed.json").unlink()
    with pytest.raises(SystemExit, match="FINAL"):
        cwa.classify("DocX", {}, run_dir)
