"""Tests for the CORD value parsers.

Every case below is a value that ACTUALLY OCCURS in CORD-v2, with its census count where the
shape is common. The point of the file is that the thousands-separator rule can never be
"simplified" back into float() without a red test: 4,385 corpus values are comma-thousands and
1,500 are dot-thousands, so a naive parse silently divides most of the corpus by 1000.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from datasets import cord_parsers as C          # noqa: E402
from datasets import fatura_parsers as F        # noqa: E402


# ------------------------------------------------------------------ the separator rule
@pytest.mark.parametrize("raw,expected", [
    # the two thousands conventions, both present in the same corpus (4385 and 1500 values)
    ("120,000",      "120000.00"),
    ("25.000",       "25000.00"),
    ("58,000",       "58000.00"),
    ("165,000",      "165000.00"),
    # both separators: the LAST one is the decimal point (131 and 101 values)
    ("53,636.00",    "53636.00"),
    ("8.500,00",     "8500.00"),
    ("Rp 35,636.36", "35636.36"),
    ("Rp 9.500,00",  "9500.00"),
    # one separator, 1-2 trailing digits: a decimal (56 and 17 values)
    ("0.00",         "0.00"),
    ("0,00",         "0.00"),
    ("210.50",       "210.50"),
    # plain integers (690 values)
    ("0",            "0.00"),
    ("116000",       "116000.00"),
    # one separator, exactly 3 trailing digits, >3 leading. Resolved as thousands, by the
    # corpus convention rather than by guess. Only 3 values; documented in the module.
    ("1071.000",     "1071000.00"),
    ("1178.100",     "1178100.00"),
    # repeated separator with a 1-2 digit tail: a mistyped mixed form (5 and 2 values)
    ("121.000.00",   "121000.00"),
    ("39,200,00",    "39200.00"),
    # space as the thousands separator (6 values)
    ("19 000",       "19000.00"),
    ("154 000",      "154000.00"),
])
def test_separator_rule(raw, expected):
    assert C.cord_money(raw) == expected


def test_thousands_is_not_a_decimal():
    """The whole reason this module exists. float('120,000'.replace(',','.')) is 120.0."""
    assert C.cord_money("120,000") == "120000.00"
    assert C.cord_money("25.000") == "25000.00"


# ------------------------------------------------------------------ decoration stripping
@pytest.mark.parametrize("raw,expected", [
    ("Rp 116000.00",  "116000.00"),   # 98 values
    ("Rp. 22.000",    "22000.00"),    # 35
    ("Rp.34.000",     "34000.00"),    # 6
    ("RP36362",       "36362.00"),    # 1
    ("@13000",        "13000.00"),    # 41
    ("@30,000",       "30000.00"),    # 29
    ("@ 5909",        "5909.00"),     # 4
    ("*46000",        "46000.00"),    # 5
    (".48000",        "48000.00"),    # 1
    ("[51,000]",      "51000.00"),    # 12
    ("39,800)",       "39800.00"),    # 5
    (":0",            "0.00"),        # 1
    ("129000-",       "129000.00"),   # 1
    ("TOTAL 47,499",  "47499.00"),    # a glued label: take the last numeric token
])
def test_decoration_is_stripped(raw, expected):
    assert C.cord_money(raw) == expected


def test_parenthesised_is_negative_but_abs_is_positive():
    assert C.cord_money("(0)") == "0.00"
    assert C.cord_money("( 1,236)") == "-1236.00"
    assert C.cord_money_abs("( 1,236)") == "1236.00"


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("raw", [
    "---",            # 10 values: a printed dash, not an amount
    "-",              # 6
    "10.000%",        # a rate, not an amount
    "Discount (0%)",  # a rate with a label
    "Pb1 10% 7,000",  # tax caption glued to an amount; the % makes it unsafe to guess
    "57,0000",        # four trailing digits fits neither convention
    "385,0000",
    None,
    "",
])
def test_refuses_rather_than_guesses(raw):
    """A refusal becomes a published exclusion with a reason. A guess becomes a wrong number
    nobody can see. 39 of 7,050 corpus values land here, and 32 of those are list-valued."""
    assert C.cord_money(raw) is None


def test_list_value_is_refused():
    """CORD emits a list when the receipt prints the same category twice. Two subtotals is a
    genuinely ambiguous fact, so the adapter must exclude it rather than pick one."""
    assert C.cord_money(["20,000", "20,000"]) is None
    assert C.cord_money(["74,000", "100,000"]) is None


# ------------------------------------------------------------------ discounts
@pytest.mark.parametrize("raw,expected", [
    ("800-",     "800.00"),
    ("1,400 -",  "1400.00"),
    ("1,260-",   "1260.00"),
])
def test_discount_is_a_positive_magnitude(raw, expected):
    """A trailing '-' is the Indonesian no-cents marker on some receipts and a minus on others.
    The schema wants a magnitude, so the ambiguity is resolved by taking one — the same choice
    fatura_parsers.money_abs makes, and for the same postprocessor reason."""
    assert C.cord_money_abs(raw) == expected


# ------------------------------------------------------------------ quantity
@pytest.mark.parametrize("raw,expected", [
    ("2", "2.00"), ("1X", "1.00"), ("x1", "1.00"), ("1x", "1.00"),
    ("2.00", "2.00"), ("1,000", "1000.00"),
])
def test_quantity_strips_the_x(raw, expected):
    """410 of 2,331 menu.cnt values carry an x, on either side."""
    assert C.cord_qty(raw) == expected


def test_quantity_refuses_a_bare_x():
    assert C.cord_qty("x") is None


# ------------------------------------------------------------------ percent
def test_percent_only_when_there_is_a_percent_sign():
    assert C.cord_percent("10%") == "10.00"
    assert C.cord_percent("0%") == "0.00"
    # 99 of 101 menu.discountprice values are amounts, not rates. Reading one as a rate would
    # score a 1,400-rupiah discount as a 1,400 percent discount.
    assert C.cord_percent("1,400 -") is None
    assert C.cord_percent("800-") is None


# ------------------------------------------------------------------ text
def test_text_joins_a_two_line_name():
    """A list here means CORD read the name across two lines. The row still names one product,
    so joining keeps a scoreable row that dropping would remove.

    Case is PRESERVED, the same as passthrough_strip. Ground truth stores the printed form and
    the matcher case-folds both sides at compare time; casefolding at storage time would
    deviate from the invoice convention and lowercase every name in the review export.
    """
    assert C.cord_text(["TRIPPLE", "CHEESE"]) == "TRIPPLE CHEESE"
    assert C.cord_text("  BLUS   WANITA ") == "BLUS WANITA"
    assert C.cord_text(["TRIPPLE", "CHEESE"]) == C.get("passthrough_strip")("TRIPPLE CHEESE")


# ------------------------------------------------------------------ isolation from FATURA
def test_cord_registry_does_not_mutate_the_invoice_one():
    """The invoice parser table is a frozen contract. cord_parsers.get() falls back to it and
    must never add to it, or a FATURA map could silently resolve a CORD parser name."""
    for name in ("cord_money", "cord_money_abs", "cord_qty", "cord_percent", "cord_text"):
        assert name not in F.known(), f"{name} leaked into the FATURA parser registry"
        assert C.get(name) is not None


def test_cord_get_falls_back_to_shared_parsers():
    assert C.get("passthrough_strip") is F.get("passthrough_strip")
    assert C.get("money_amount") is F.get("money_amount")
