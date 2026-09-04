"""Row alignment is the only part of the scorer where a plausible implementation is wrong.

Index-wise comparison, greedy matching and "score every predicted row against the nearest GT
row" all produce numbers that look reasonable and mean nothing. Each test below pins one of
those failures.
"""
from __future__ import annotations

import pytest

from core import rows as R

TARGETS = {
    "description": ("description",),
    "itemCode": ("itemCode",),
    "quantity": ("quantity",),
    "unitPrice": ("unitPrice",),
    "lineTotal": ("lineTotalExcludingTax", "lineTotalIncludingTax"),
}
KEYS = ("description", "descriptionAlt")
LEAF_TYPES = {"description": "str", "itemCode": "str", "quantity": "numeric",
              "unitPrice": "numeric", "lineTotalExcludingTax": "numeric",
              "lineTotalIncludingTax": "numeric"}


def gt(desc, qty=None, price=None, total=None, alt=None, code=None):
    return {"description": desc, "descriptionAlt": alt, "itemCode": code,
            "quantity": qty, "unitPrice": price, "lineTotal": total}


def pred(desc=None, qty=None, price=None, lte=None, lti=None, code=None):
    return {"description": desc, "itemCode": code, "quantity": qty, "unitPrice": price,
            "lineTotalExcludingTax": lte, "lineTotalIncludingTax": lti}


def _score(g, p):
    return R.score_rows(g, p, list_path="lineItems", gt_match_keys=KEYS,
                        row_targets=TARGETS, leaf_types=LEAF_TYPES, doc_id="d")


# --------------------------------------------------------------- the dropped-row case
def test_a_dropped_middle_row_costs_one_row_not_all_the_rest():
    """The whole reason this module exists. Index-wise, dropping row 2 of 4 marks rows 2,3,4
    wrong — three failures from one omission, and the count then measures row order."""
    g = [gt("Alpha", "1.00"), gt("Bravo", "2.00"), gt("Charlie", "3.00"), gt("Delta", "4.00")]
    p = [pred("Alpha", "1.00"), pred("Charlie", "3.00"), pred("Delta", "4.00")]
    out = _score(g, p)
    s = out["summary"]
    assert s["n_matched"] == 3
    assert s["row_recall"] == pytest.approx(0.75)
    assert s["row_precision"] == pytest.approx(1.0)
    assert out["alignment"].unmatched_gt == [1]
    # and every cell of the three surviving rows is still credited
    assert s["n_cells_correct"] == s["n_cells"]


def test_invented_rows_land_in_precision_and_nowhere_else():
    """Without a precision term a model that emits every plausible row scores perfect."""
    g = [gt("Alpha", "1.00")]
    p = [pred("Alpha", "1.00"), pred("Ghost", "9.00"), pred("Phantom", "8.00")]
    s = _score(g, p)["summary"]
    assert s["row_recall"] == pytest.approx(1.0)
    assert s["row_precision"] == pytest.approx(1 / 3)
    assert s["row_f1"] == pytest.approx(0.5)
    assert not s["table_exact"]


# --------------------------------------------------------------- the greedy failure
def test_identical_descriptions_are_paired_by_their_numbers_not_arbitrarily():
    """DocILE100 has six consecutive rows reading 'STEWART JENKINS AD 5' differing only in
    quantity and price. Greedy matching pairs them in encounter order and then scores the
    wrong cells against each other; exact assignment with a numeric tie-break does not."""
    g = [gt("STEWART JENKINS AD 5", "1.00", "5.00", "5.00"),
         gt("STEWART JENKINS AD 5", "2.00", "20.00", "40.00"),
         gt("STEWART JENKINS AD 5", "3.00", "20.00", "60.00")]
    # deliberately shuffled relative to the GT order
    p = [pred("STEWART JENKINS AD 5", "3.00", "20.00", "60.00"),
         pred("STEWART JENKINS AD 5", "1.00", "5.00", "5.00"),
         pred("STEWART JENKINS AD 5", "2.00", "20.00", "40.00")]
    out = _score(g, p)
    assert out["summary"]["n_matched"] == 3
    assert out["summary"]["n_cells_correct"] == out["summary"]["n_cells"], (
        "rows with identical text were paired without regard to their numbers")
    assert out["summary"]["table_exact"]


def test_assignment_is_exact_not_greedy():
    """A matrix where the greedy first choice forces a worse total. Greedy takes (0,0) at 1.0
    and leaves row 1 unmatched; the optimal assignment matches both."""
    g = [gt("Alpha Beta"), gt("Alpha Beta Gamma")]
    p = [pred("Alpha Beta Gamma"), pred("Alpha Beta")]
    al = R.align(g, p, gt_match_keys=KEYS, row_targets=TARGETS)
    assert len(al.matches) == 2, "an exact assignment matches both rows"
    assert {(m.gt_index, m.pred_index) for m in al.matches} == {(0, 1), (1, 0)}


def test_hungarian_matches_brute_force_on_random_matrices():
    """Cross-checked against every permutation rather than a hand-computed constant: a
    hand-computed expectation is exactly as likely to be wrong as the implementation, and on
    the first draft of this file it was."""
    import itertools
    import random

    rnd = random.Random(20260903)
    for n in (1, 2, 3, 4, 5):
        for _ in range(40):
            cost = [[rnd.randrange(0, 20) * 1.0 for _ in range(n)] for _ in range(n)]
            got = sum(cost[i][j] for i, j in R._hungarian(cost))
            best = min(sum(cost[i][p[i]] for i in range(n))
                       for p in itertools.permutations(range(n)))
            assert got == pytest.approx(best), f"n={n} cost={cost}"


def test_hungarian_beats_greedy_where_they_differ():
    """The greedy trap: taking the cheapest cell first (1.0 at (0,0)) forces 9.0 later."""
    cost = [[1.0, 2.0, 3.0],
            [2.0, 4.0, 6.0],
            [3.0, 6.0, 9.0]]
    got = sum(cost[i][j] for i, j in R._hungarian(cost))
    assert got == pytest.approx(10.0)      # (0,2)+(1,1)+(2,0); greedy gives 1+4+9 = 14


def test_hungarian_handles_rectangular_both_ways():
    tall = [[1.0, 9.0], [9.0, 1.0], [5.0, 5.0]]
    wide = [[1.0, 9.0, 5.0], [9.0, 1.0, 5.0]]
    assert len(R._hungarian(tall)) == 2
    assert len(R._hungarian(wide)) == 2
    assert R._hungarian([]) == []


# --------------------------------------------------------------- the annotation drift
def test_a_row_matches_on_either_gt_text_column():
    """DocILE100's radio-log family puts the generic '60 Spot' in itemName and the real
    description in itemCode. Matching on itemName alone would lose all 44 rows."""
    g = [gt("60 Spot", alt='20AFSCMEMTOR1R "Bankroll" Radio :60', total="16.45")]
    p = [pred('20AFSCMEMTOR1R "Bankroll" Radio :60', lte="16.45")]
    out = _score(g, p)
    assert out["summary"]["n_matched"] == 1
    assert out["summary"]["matched_on"] == ["descriptionAlt"], (
        "the report must say which column matched, or the drift becomes invisible")


def test_getting_the_numbers_wrong_does_not_un_find_the_row():
    """Identity and cell accuracy are separate measurements. If a wrong quantity dropped the
    row from the match, row recall and field accuracy would stop being independent and a
    number could improve by getting MORE cells wrong."""
    g = [gt("Alpha", "1.00", "5.00", "5.00")]
    p = [pred("Alpha", "99.00", "99.00", lte="99.00")]
    out = _score(g, p)
    assert out["summary"]["row_recall"] == pytest.approx(1.0)
    by_path = {c["path"]: c["ok"] for c in out["cells"]}
    assert by_path["lineItems[].description"] is True
    assert by_path["lineItems[].quantity"] is False
    assert by_path["lineItems[].unitPrice"] is False
    assert by_path["lineItems[].lineTotal"] is False
    assert out["summary"]["row_exact_rate"] == pytest.approx(0.0)


# --------------------------------------------------------------- union targets
def test_a_line_total_routed_to_the_including_tax_leaf_still_counts():
    """One GT value, two schema homes. lineTotalExcludingTax's description says to use it when
    no tax applies on the line, but the instruction is conditional and a model that read the
    row as tax-inclusive has not got the NUMBER wrong."""
    g = [gt("Alpha", total="12.34")]
    out = _score(g, [pred("Alpha", lti="12.34")])
    cell = next(c for c in out["cells"] if c["path"].endswith("lineTotal"))
    assert cell["ok"] is True
    assert cell["matched_leaf"] == "lineTotalIncludingTax", (
        "the winning leaf must be recorded so routing behaviour stays measurable")


def test_cells_are_scored_only_where_the_gt_states_a_value():
    """DocILE100 states a quantity on 264 of 450 rows. A per-column denominator of 450 would
    overstate the quantity denominator by 40% and count silence as failure."""
    g = [gt("Alpha", qty=None, price="5.00", total="5.00")]
    out = _score(g, [pred("Alpha", "7.00", "5.00", lte="5.00")])
    paths = [c["path"] for c in out["cells"]]
    assert "lineItems[].quantity" not in paths
    assert out["summary"]["n_cells"] == 3      # description, unitPrice, lineTotal


# --------------------------------------------------------------- degenerate shapes
def test_no_predicted_rows_is_zero_recall_and_undefined_precision():
    s = _score([gt("Alpha")], [])["summary"]
    assert s["row_recall"] == pytest.approx(0.0)
    assert s["row_precision"] is None, "0/0 must be undefined, not 0.0"
    assert not s["table_exact"]


def test_no_gt_rows_never_reports_a_table_as_exact():
    """3 of 100 DocILE100 documents have no itemised table — one verified as a remittance
    advice. table_exact must not be True for a table that was never asked for."""
    s = _score([], [pred("Ghost")])["summary"]
    assert s["row_recall"] is None
    assert s["row_precision"] == pytest.approx(0.0)
    assert not s["table_exact"]


def test_table_exact_requires_no_omission_and_no_invention():
    g = [gt("Alpha", "1.00"), gt("Bravo", "2.00")]
    assert _score(g, [pred("Alpha", "1.00"), pred("Bravo", "2.00")])["summary"]["table_exact"]
    assert not _score(g, [pred("Alpha", "1.00")])["summary"]["table_exact"]
    assert not _score(g, [pred("Alpha", "1.00"), pred("Bravo", "2.00"),
                          pred("Ghost", "3.00")])["summary"]["table_exact"]
