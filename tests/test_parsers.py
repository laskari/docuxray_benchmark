"""Parser tests -- every parser named in fatura_field_map.yaml, on real FATURA value shapes."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from datasets import fatura_parsers as P
from core.normalize import AMBIGUOUS_CURRENCY


def run(name, raw, key=""):
    return P.get(name)(raw, key=key)


def test_every_parser_referenced_by_the_rule_table_is_registered():
    from datasets.fatura_invoice import check_parsers_exist
    check_parsers_exist()


def test_unknown_parser_fails_loudly():
    with pytest.raises(KeyError, match="unknown parser"):
        P.get("no_such_parser")


# ------------------------------------------------------------------ TAX: one label, four paths

TAX = "VAT (3.88%): 28.18 EUR"


def test_tax_label_expands_to_four_values():
    assert run("tax_name", TAX) == "VAT"
    assert run("tax_pct", TAX) == "3.88"
    assert run("tax_amount", TAX) == "28.18"
    assert run("money_currency", TAX) == "EUR"


def test_tax_amount_never_picks_up_the_rate():
    # The rate sits inside parens before the colon; a naive 'last number' would work here but
    # not on 'VAT (5.50%): 60.39' if the currency were absent. Both are covered.
    assert run("tax_amount", "VAT (5.50%): 60.39 USD") == "60.39"
    assert run("tax_amount", "VAT (5.50%): 60.39") == "60.39"


@pytest.mark.parametrize("raw,name", [
    ("VAT (3.88%): 28.18 EUR", "VAT"),
    ("Sales Tax (7%): 1.20", "Sales Tax"),
    ("28.18", None),
])
def test_tax_name(raw, name):
    assert run("tax_name", raw) == name


# ------------------------------------------------------------------ DISCOUNT

def test_discount_is_stored_as_positive_magnitude():
    # invoice_postprocessor._normalize_totals flips a negative discountTotal to abs(). GT must
    # match that convention or the postprocessed arm scores worse than bare extraction as a
    # pure sign artifact.
    assert run("money_abs", "(1.85%): (-) 13.42") == "13.42"
    assert run("pct_in_parens", "(1.85%): (-) 13.42") == "1.85"


# ------------------------------------------------------------------ GST: rate lives in the key

@pytest.mark.parametrize("key,rate", [
    ("GST(18%)", "18.00"),
    ("GST(1%)", "1.00"),
    ("GST( 12.5 %)", "12.50"),
    ("TAX", None),
    ("", None),
])
def test_rate_from_key(key, rate):
    assert run("rate_from_key", "230.72", key=key) == rate


def test_gst_value_is_the_amount():
    assert run("money_amount", "230.72", key="GST(18%)") == "230.72"


def test_literal_parser_uses_the_map_value():
    assert P.get("literal")(None, key="GST(18%)", value="GST") == "GST"


# ------------------------------------------------------------------ money / currency / dates

@pytest.mark.parametrize("raw,expected", [
    ("734.33 EUR", "734.33"),
    ("BALANCE_DUE : 481.84 $", "481.84"),
    ("1,098.21 USD", "1098.21"),
])
def test_money_amount(raw, expected):
    assert run("money_amount", raw) == expected


def test_money_currency_refuses_to_guess_dollar():
    assert run("money_currency", "266.80 $") == AMBIGUOUS_CURRENCY


def test_date_to_iso_is_strict_to_fatura_format():
    assert run("date_to_iso", "20-Mar-2008") == "2008-03-20"
    assert run("date_to_iso", "2008-03-20") is None       # not FATURA's printed form
    assert run("date_to_iso", "Due Date : 08-Mar-2020") is None


# ------------------------------------------------------------------ text

def test_passthrough_preserves_case_and_collapses_whitespace():
    assert run("passthrough_strip", "  INV/30-14/832 ") == "INV/30-14/832"
    assert run("passthrough_strip", "Denise   Perez") == "Denise Perez"
    assert run("passthrough_strip", None) is None


def test_address_whole_flattens_newlines():
    assert run("address_whole", "16424 Timothy Mission\nMarkville, AK 58294 US") == \
        "16424 timothy mission, markville, ak 58294 us"


def test_phone_normalise():
    assert run("phone_normalise", "+(352)259-8443") == "+3522598443"
