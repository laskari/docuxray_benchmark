"""End-to-end scorer validation against predictions with KNOWN answers.

Predictions are constructed FROM the ground truth, then broken in specific, counted ways. If
the scorer's numbers disagree with the breakages, the scorer is wrong.
"""
import copy, json, pathlib, shutil, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from core.canonical import read_jsonl
from core.normalize import _Absent
from core import metrics

_ROOT = pathlib.Path(__file__).resolve().parent.parent
GT = _ROOT / "gt" / "invoice" / "fatura" / "ground_truth.jsonl"
pytestmark = pytest.mark.skipif(not GT.exists(), reason="run scripts/build_gt.py first")

NUMERIC = {"totals.totalIncludingTax", "totals.subtotal", "totals.taxAmount",
           "totals.taxPercentage", "totals.discountTotal", "totals.discountPercentage"}


def perfect_prediction(rec) -> dict:
    """A prediction that should score 100%: GT values in the shapes the pipeline emits."""
    out: dict = {}
    for path, val in rec.gt.items():
        if path == "lineItems" or isinstance(val, _Absent):
            continue
        parts = path.split(".")
        target = out
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        leaf = parts[-1]
        if path in NUMERIC:
            target[leaf] = {"originalValue": val, "normalizedValue": float(val)}
        elif leaf == "addressStructured":
            target[leaf] = {"address": val, "city": None, "state": None,
                            "postal_code": None, "country": None}
        elif path == "invoiceInfo.noteText":
            out.setdefault("invoiceInfo", {})["customerMemo"] = val
        else:
            target[leaf] = val
    return {"invoiceOutputData": out}


def _set(pred: dict, path: str, value):
    t = pred["invoiceOutputData"]
    for p in path.split(".")[:-1]:
        t = t.setdefault(p, {})
    t[path.split(".")[-1]] = value


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    gt = {r.doc_id: r for r in read_jsonl(str(GT))}
    plan = json.loads((_ROOT / "gt" / "invoice" / "fatura" / "smoke_51.json").read_text())["doc_ids"]
    # documents that annotate BOTH fields we intend to break, so the breakages are scoreable
    usable = [d for d in plan
              if {"invoiceInfo.issueDate", "totals.totalIncludingTax"} <= gt[d].annotated_fields][:4]
    assert len(usable) == 4

    run = pathlib.Path(tmp_path_factory.mktemp("run"))
    (run / "raw").mkdir()
    for i, d in enumerate(usable):
        rec = gt[d]
        base = perfect_prediction(rec)
        if i == 1:                                   # one wrong numeric, in both arms
            _set(base, "totals.totalIncludingTax",
                 {"originalValue": "999.99", "normalizedValue": 999.99})
        if i == 2 and rec.absent_fields:             # one hallucination, in both arms
            _set(base, sorted(rec.absent_fields)[0], "INVENTED")
        raw, final = copy.deepcopy(base), copy.deepcopy(base)
        judge = {"issues": {}}
        if i == 3:
            # wrong in RAW, right in FINAL  -> a fix
            _set(raw, "invoiceInfo.issueDate", "BROKEN")
            # right in RAW, wrong in FINAL  -> a harm
            _set(final, "totals.totalIncludingTax",
                 {"originalValue": "1.00", "normalizedValue": 1.0})
            judge = {"issues": {"invoiceInfo": [{"field": "issueDate"}]}}
        (run / "raw" / f"{d}.json").write_text(json.dumps({
            "doc_id": d, "ok": True, "cached": False, "error": None,
            "arms": {"RAW": raw, "FINAL": final}, "judge": judge,
            "tokens": {"total": 0}, "cost_usd": 0.0, "latency_ms": {}}))
    # dataset is explicit because doc_type alone stopped identifying the ground truth once a
    # second invoice dataset (DocILE100) was registered.
    (run / "manifest.json").write_text(json.dumps({
        "dataset": "fatura",
        "run_id": "test", "models": {"extraction": "t", "judge": "t"},
        "total_cost_usd": 0.0, "git": {}, "field_map_version": "test"}))
    return metrics.score_run(run), usable, gt


def test_perfect_predictions_score_near_perfect(scored):
    res, _, _ = scored
    # 4 documents, of which one has a corrupted total and one a hallucination
    assert res["arms"]["RAW"]["micro_recall"] > 0.95


def test_the_one_corrupted_numeric_is_caught(scored):
    res, _, _ = scored
    states = {}
    for f in res["arms"]["RAW"]["per_field"]:
        if f["path"] == "totals.totalIncludingTax":
            states = f["states"]
    assert states.get("wrong") == 1, states


def test_the_one_hallucination_is_counted_separately(scored):
    res, _, _ = scored
    assert res["arms"]["RAW"]["hallucination_count"] == 1
    # and it is NOT folded into recall
    assert res["arms"]["RAW"]["correct_null_rate"] is not None


def test_judge_fix_and_harm_are_both_counted(scored):
    res, _, _ = scored
    j = res["judge"]
    assert j["fields_fixed"] == 1, j          # issueDate wrong in RAW, right in FINAL
    assert j["fields_harmed"] == 1, j         # total right in RAW, wrong in FINAL
    assert j["net_lift_fields"] == 0
    assert j["harm_rate"] > 0


def test_judge_detector_matrix_sees_the_flag(scored):
    res, _, _ = scored
    c = res["judge"]["confusion"]
    assert c["tp"] >= 1, c                    # issueDate was flagged AND was wrong


def test_unannotated_paths_are_never_scored(scored):
    """A field GT cannot speak to must not appear, however wrong the prediction is."""
    res, usable, gt = scored
    scored_paths = {f["path"] for f in res["arms"]["RAW"]["per_field"]}
    annotated = set().union(*[gt[d].annotated_fields for d in usable])
    assert scored_paths <= annotated


def test_confidence_intervals_come_from_templates(scored):
    res, _, _ = scored
    a = res["arms"]["RAW"]
    assert a["n_templates"] == 4
    lo, hi = a["micro_ci95"]
    assert lo is not None and lo <= a["micro_recall"] <= hi
