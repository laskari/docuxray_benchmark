"""The three-arm contract: what the postprocessor did must not be credited to the judge.

Arm RAW carries only `originalValue` -- NumericValue sets extra="forbid", so the model cannot
emit a normalised number -- while arm FINAL carries `normalizedValue` written by the product's
own parser. A RAW -> FINAL comparison therefore measures the judge AND that parser difference
together. Measured on runs/main (1,000 documents): 137 fields looked fixed against RAW, of
which 73 were the postprocessor's negative-discount rule on `totals.discountTotal`, where
FATURA prints "(-) 9.39" and the correct answer is 9.39. The judge's real net lift was +16,
not +89.

RAW_POSTPROCESSED is RAW put through the same postprocessor arm FINAL ends with. It costs
nothing, it is never shipped and no model ever sees it, and it exists so that both sides of
the judge comparison sit on the same side of that parser.

These tests fix five things in place:
  * fixed/harmed are measured against RAW_POSTPROCESSED, the deterministic stage is reported
    separately, and the two decompose EXACTLY into the old RAW -> FINAL number;
  * the detector confusion matrix stays on RAW, because RAW is what the judge was shown;
  * the postprocessor's destructive edits are counted, as a standing guard;
  * a run whose arms cover different documents is refused, not published;
  * a run without the derived arm still scores, and says what it is missing.
"""
import copy, json, pathlib, sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from core.canonical import read_jsonl                                    # noqa: E402
from core import metrics                                                 # noqa: E402
from test_metrics import GT, _set, perfect_prediction                    # noqa: E402

pytestmark = pytest.mark.skipif(not GT.exists(), reason="run scripts/build_gt.py first")

MANIFEST = {"dataset": "fatura", "run_id": "test",
            "models": {"extraction": "t", "judge": "t"},
            "total_cost_usd": 0.0, "git": {}, "field_map_version": "test"}
BREAKABLE = {"invoiceInfo.issueDate", "totals.totalIncludingTax", "totals.discountTotal"}


def _docs(n):
    """n documents from the smoke plan that annotate every field these tests break."""
    gt = {r.doc_id: r for r in read_jsonl(str(GT))}
    plan = json.loads(
        (_ROOT / "gt" / "invoice" / "fatura" / "smoke_51.json").read_text())["doc_ids"]
    usable = [d for d in plan if BREAKABLE <= gt[d].annotated_fields][:n]
    assert len(usable) == n, f"only {len(usable)} usable documents in the smoke plan"
    return gt, usable


def _write(run: pathlib.Path, bodies: dict) -> pathlib.Path:
    (run / "raw").mkdir(parents=True, exist_ok=True)
    for doc_id, body in bodies.items():
        (run / "raw" / f"{doc_id}.json").write_text(json.dumps({
            "doc_id": doc_id, "ok": True, "cached": False, "error": None,
            "tokens": {"total": 0}, "cost_usd": 0.0, "latency_ms": {}, **body}))
    (run / "manifest.json").write_text(json.dumps(MANIFEST))
    return run


def _build(run: pathlib.Path):
    """One document per effect, so every count below is exact and hand-checkable.

    doc 0  clean everywhere.
    doc 1  the discountTotal pattern: RAW prints "(-) x" with no normalizedValue, and the
           postprocessor parses it to x. Wrong in RAW, right in RAW_POSTPROCESSED and FINAL,
           and the judge says nothing about it.
           -> postprocessing fixed +1, judge fixed 0. Against RAW this scored as a judge fix.
    doc 2  a real judge fix: wrong in RAW and RAW_POSTPROCESSED, right in FINAL, and flagged.
           -> judge fixed +1.
    doc 3  a real judge harm: right in RAW and RAW_POSTPROCESSED, wrong in FINAL.
           -> judge harmed +1.
    """
    gt, docs = _docs(4)
    bodies = {}
    for i, doc_id in enumerate(docs):
        rec = gt[doc_id]
        base = perfect_prediction(rec)
        raw, raw_pp, final = (copy.deepcopy(base) for _ in range(3))
        judge = {"issues": {}}

        if i == 1:
            truth = rec.gt["totals.discountTotal"]
            _set(raw, "totals.discountTotal", {"originalValue": f"(-) {truth}"})
            for arm in (raw_pp, final):
                _set(arm, "totals.discountTotal",
                     {"originalValue": f"(-) {truth}", "normalizedValue": float(truth)})
        if i == 2:
            for arm in (raw, raw_pp):
                _set(arm, "invoiceInfo.issueDate", "BROKEN")
            judge = {"issues": {"invoiceInfo": [{"field": "issueDate"}]}}
        if i == 3:
            _set(final, "totals.totalIncludingTax",
                 {"originalValue": "1.00", "normalizedValue": 1.0})

        bodies[doc_id] = {"arms": {"RAW": raw, "RAW_POSTPROCESSED": raw_pp, "FINAL": final},
                          "judge": judge}
    return _write(run, bodies), docs


@pytest.fixture(scope="module")
def three_arms(tmp_path_factory):
    run, docs = _build(pathlib.Path(tmp_path_factory.mktemp("three")))
    return metrics.score_run(run), docs, run


def test_all_three_arms_are_scored_on_the_same_documents(three_arms):
    res, docs, _ = three_arms
    assert set(res["arms"]) == {"RAW", "RAW_POSTPROCESSED", "FINAL"}
    assert res["arm_denominators_equal"]
    assert set(res["arm_denominators"].values()) == {len(docs)}
    # pipeline order, so the report reads forwards rather than alphabetically
    assert list(res["arms"]) == ["RAW", "RAW_POSTPROCESSED", "FINAL"]


def test_the_parser_difference_is_credited_to_the_postprocessor_not_the_judge(three_arms):
    res, _, _ = three_arms
    pp, j = res["postprocessing"], res["judge"]

    assert (pp["from"], pp["to"]) == ("RAW", "RAW_POSTPROCESSED")
    assert pp["fields_fixed"] == 1          # doc 1, the "(-) x" discount
    assert pp["fields_harmed"] == 0

    assert j["baseline_arm"] == "RAW_POSTPROCESSED"
    assert j["fields_fixed"] == 1           # doc 2 only -- NOT doc 1
    assert j["fields_harmed"] == 1          # doc 3
    assert j["net_lift_fields"] == 0


def test_the_two_stages_decompose_exactly_into_the_old_number(three_arms):
    """The arithmetic proof that this is a RE-ATTRIBUTION, not a change of measurement.

    The same run scored twice -- baseline RAW_POSTPROCESSED, then forced back to RAW -- must
    give the same total: postprocessing.fixed + judge.fixed equals the old RAW -> FINAL fixed
    count, harmed is identical either way, and the detector matrix does not move at all.
    On runs/main: 73 + 64 = 137 fixed, 48 harmed both ways.
    """
    res, _, run = three_arms
    old = metrics.score_run(run, scoring={"judge_baseline_arm": "RAW",
                                          "judge_detector_arm": "RAW"})
    assert old["judge"]["baseline_arm"] == "RAW"
    assert (res["postprocessing"]["fields_fixed"] + res["judge"]["fields_fixed"]
            == old["judge"]["fields_fixed"])
    assert res["judge"]["fields_harmed"] == old["judge"]["fields_harmed"]
    assert res["judge"]["confusion"] == old["judge"]["confusion"]
    metrics.score_run(run)                       # restore results.json for any later reader


def test_the_detector_matrix_stays_on_the_arm_the_judge_was_shown(three_arms):
    """Scoring the flags against RAW_POSTPROCESSED shrinks the 'wrong before' denominator and
    flatters detector recall -- on runs/main it moved 29.9% -> 40.9% with the judge doing
    nothing differently. doc 1 is wrong in RAW and unflagged, so it must count as a miss."""
    res, _, _ = three_arms
    j = res["judge"]
    assert j["detector_arm"] == "RAW"
    assert res["scoring"]["judge_detector_arm"] == "RAW"
    assert j["confusion"]["tp"] == 1         # doc 2: flagged and wrong in RAW
    assert j["confusion"]["fn"] >= 1         # doc 1: wrong in RAW, silent


def test_the_postprocessors_destructive_edits_are_counted(tmp_path):
    """The standing guard. The postprocessor also adjudicates -- clearing invalid emails,
    out-of-range percentages, anything the format contract rejects -- and on runs/main that is
    96 leaves. If it grows, the judge's measured lift moves for an unrelated reason, and
    nothing else in the harness would say so.

    The paths must be SCHEMA paths. Reporting them with the `invoiceOutputData` wrapper still
    attached would make them match nothing else in the report -- and whether a run stores its
    arms wrapped is an accident of which code path wrote it.
    """
    gt, docs = _docs(1)
    doc_id = docs[0]
    base = perfect_prediction(gt[doc_id])
    raw, raw_pp = copy.deepcopy(base), copy.deepcopy(base)
    _set(raw, "invoiceInfo.paymentTerms", {"raw_text": "net 30", "term_type": "UNKNOWN"})
    _set(raw_pp, "invoiceInfo.paymentTerms", {"raw_text": "net 30", "term_type": None})
    run = _write(tmp_path / "nulls", {doc_id: {
        "arms": {"RAW": raw, "RAW_POSTPROCESSED": raw_pp, "FINAL": copy.deepcopy(raw_pp)},
        "judge": {"issues": {}}}})

    nulled = metrics.score_run(run)["postprocessing"]["leaves_nulled"]
    assert nulled["total"] == 1
    assert nulled["n_documents"] == 1
    assert list(nulled["by_field"]) == ["invoiceInfo.paymentTerms.term_type"]
    assert not any(k.startswith("invoiceOutputData") for k in nulled["by_field"])


def test_a_run_whose_arms_cover_different_documents_is_refused(tmp_path):
    """runs/main scored RAW and FINAL on 1,000 documents and RAW_POSTPROCESSED on 969, and
    published all three in one results.md as though they were comparable."""
    gt, docs = _docs(2)
    bodies = {}
    for i, doc_id in enumerate(docs):
        base = perfect_prediction(gt[doc_id])
        arms = {"RAW": copy.deepcopy(base), "FINAL": copy.deepcopy(base)}
        if i == 0:                                   # only ONE document carries the arm
            arms["RAW_POSTPROCESSED"] = copy.deepcopy(base)
        bodies[doc_id] = {"arms": arms, "judge": {"issues": {}}}
    run = _write(tmp_path / "mismatch", bodies)

    with pytest.raises(metrics.ArmDenominatorMismatch) as e:
        metrics.score_run(run)
    assert "RAW_POSTPROCESSED" in str(e.value) and "missing 1" in str(e.value)
    assert not (run / "results.json").exists(), "nothing may be written on a refusal"

    res = metrics.score_run(run, allow_arm_mismatch=True)
    assert res["arm_denominators_equal"] is False
    assert "NOT EQUAL" in (run / "results.md").read_text(encoding="utf-8")


def test_an_old_run_without_the_derived_arm_still_scores(tmp_path, capsys):
    """Every run built before this arm existed must keep scoring -- falling back to RAW, and
    saying so, rather than raising or silently reporting nothing."""
    gt, docs = _docs(2)
    bodies = {}
    for doc_id in docs:
        base = perfect_prediction(gt[doc_id])
        bodies[doc_id] = {"arms": {"RAW": copy.deepcopy(base), "FINAL": copy.deepcopy(base)},
                          "judge": {"issues": {}}}
    res = metrics.score_run(_write(tmp_path / "twoarm", bodies))
    assert "postprocessing" not in res
    assert res["judge"]["baseline_arm"] == "RAW"
    assert "add_raw_pp_arm" in capsys.readouterr().out
