"""Test the scorer before trusting it.

Every case here is a real value shape drawn from FATURA's modified_annotations, or an edge case
the normalisation rules explicitly promise to handle.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from decimal import Decimal
import pytest

from core.normalize import (
    ABSENT, AMBIGUOUS_CURRENCY, anls, levenshtein, money_to_str, normalise_address,
    normalise_currency, normalise_date, normalise_money, normalise_percent, normalise_phone,
    normalise_string, strip_label_prefix, token_f1,
)


@pytest.mark.parametrize("raw,expected", [
    ("734.33 EUR", "734.33"),
    ("266.80 $", "266.80"),
    ("1,098.21 USD", "1098.21"),
    ("1,234,567.89", "1234567.89"),
    ("(102.68)", "-102.68"),          # accounting negative
    ("(1.85%): (-) 13.42", "-13.42"), # the '(-)' marker applies to the trailing amount
    ("BALANCE_DUE : 481.84 $", "481.84"),
    ("DUE_AMOUNT : 240.38 $", "240.38"),
    ("12.5", "12.50"),                # quantised to 2dp
    ("4", "4.00"),
    ("", None),
    ("no digits here", None),
    (None, None),
])
def test_normalise_money(raw, expected):
    assert money_to_str(normalise_money(raw)) == expected


def test_money_half_up_not_bankers():
    # Python's round() would give 2.66 here. Money must round half-UP.
    assert money_to_str(normalise_money("2.665")) == "2.67"


def test_money_absent_sentinel():
    assert normalise_money(ABSENT) is None


@pytest.mark.parametrize("raw,expected", [
    ("VAT (3.88%): 28.18 EUR", Decimal("3.88")),
    ("(1.85%): (-) 13.42", Decimal("1.85")),
    ("GST(18%)", Decimal("18.00")),
    ("no percent", None),
])
def test_normalise_percent(raw, expected):
    assert normalise_percent(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("725.30 EUR", "EUR"),
    ("1098.21 USD", "USD"),
    ("266.80 $", AMBIGUOUS_CURRENCY),   # never guessed to USD
    ("100.00 £", "GBP"),
    ("100.00", None),
])
def test_normalise_currency(raw, expected):
    assert normalise_currency(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("20-Mar-2008", "2008-03-20"),
    ("03-Jan-1994", "1994-01-03"),
    ("2008-03-20", "2008-03-20"),
    ("Due Date : 08-Mar-2020", None),   # label leakage is NOT silently reinterpreted
    ("not a date", None),
    ("", None),
])
def test_normalise_date(raw, expected):
    assert normalise_date(raw) == expected


def test_date_never_guesses_us_order():
    # 05/09/2020 is parsed as 5 September, never 9 May. The convention is pinned per dataset
    # in the adapter, not guessed per document.
    assert normalise_date("05/09/2020") == "2020-09-05"


@pytest.mark.parametrize("raw,expected", [
    ("BALANCE_DUE : 481.84 $", "481.84 $"),
    ("DUE_AMOUNT : 240.38", "240.38"),
    ("VAT (3.88%): 28.18", "VAT (3.88%): 28.18"),   # paren before colon -> not a label prefix
    ("Due Date : 08-Mar-2020", "Due Date : 08-Mar-2020"),  # mixed case -> not a label prefix
    ("plain value", "plain value"),
])
def test_strip_label_prefix(raw, expected):
    assert strip_label_prefix(raw) == expected


def test_normalise_string_collapses_and_casefolds():
    assert normalise_string("  ACME   Ltd.\n\n") == "acme ltd."
    assert normalise_string("") is None


def test_normalise_address_flattens_block():
    raw = "16424 Timothy Mission\nMarkville, AK 58294 US"
    assert normalise_address(raw) == "16424 timothy mission, markville, ak 58294 us"


@pytest.mark.parametrize("raw,expected", [
    ("+(352)259-8443", "+3522598443"),
    ("(708) 462-4149", "7084624149"),
    ("n/a", None),
])
def test_normalise_phone(raw, expected):
    assert normalise_phone(raw) == expected


def test_levenshtein():
    assert levenshtein("kitten", "sitting") == 3
    assert levenshtein("", "abc") == 3
    assert levenshtein("same", "same") == 0


def test_anls_bounds_and_threshold():
    assert anls("ACME Ltd", "ACME Ltd") == 1.0
    assert anls(None, None) == 1.0
    assert anls("ACME", None) == 0.0
    assert 0.8 <= anls("ACME Ltd.", "ACME Ltd") < 1.0     # passes the 0.8 threshold
    assert anls("totally different", "ACME Ltd") < 0.8


def test_token_f1_order_insensitive():
    assert token_f1("Ward and Berg", "Berg and Ward") == 1.0
    assert token_f1("Berg", "Ward") == 0.0


# --------------------------------------------------------------------------------------
# NumericValue wire shapes -- the field's shape changes between pipeline stages
# --------------------------------------------------------------------------------------

from core.normalize import numeric_from_field  # noqa: E402


def test_numeric_from_field_arm_a_shape():
    # Raw extraction: originalValue only (NumericValue sets extra='forbid').
    value, source = numeric_from_field({"originalValue": "1,234.56"})
    assert money_to_str(value) == "1234.56"
    assert source == "originalValue"


def test_numeric_from_field_prefers_original_by_default():
    # originalValue is the default source: it is present at every stage and the postprocessor
    # only strips it, so all three arms are scored on the identical field.
    value, source = numeric_from_field({"originalValue": "1,234.56", "normalizedValue": 1234.56})
    assert money_to_str(value) == "1234.56"
    assert source == "originalValue"


def test_numeric_from_field_can_prefer_normalized_when_asked():
    value, source = numeric_from_field({"originalValue": "1,234.56", "normalizedValue": 1234.56},
                                       prefer="normalizedValue")
    assert money_to_str(value) == "1234.56"
    assert source == "normalizedValue"


def test_numeric_falls_back_when_original_is_unparseable():
    value, source = numeric_from_field({"originalValue": "n/a", "normalizedValue": 99.0})
    assert money_to_str(value) == "99.00"
    assert source == "normalizedValue"


def test_numeric_parse_disagreement_is_flagged_not_resolved():
    from core.normalize import numeric_parse_disagreement
    # '1.234' -- our parser reads 1.23, production read it as 1234 (thousands separator).
    assert numeric_parse_disagreement({"originalValue": "1.234", "normalizedValue": 1234.0})
    assert not numeric_parse_disagreement({"originalValue": "1234.00", "normalizedValue": 1234.0})
    assert not numeric_parse_disagreement({"originalValue": "1234.00"})


def test_numeric_reads_original_when_normalized_is_none():
    # The postprocessor sets normalizedValue=None when it cannot parse originalValue and raises
    # a number_unparseable warning. The printed string is still the best evidence we have.
    value, source = numeric_from_field({"originalValue": "725.30 EUR", "normalizedValue": None})
    assert money_to_str(value) == "725.30"
    assert source == "originalValue"


def test_numeric_from_field_null_wrapper():
    assert numeric_from_field({"originalValue": None, "normalizedValue": None}) == (None, None)
    assert numeric_from_field(None) == (None, None)
    assert numeric_from_field(ABSENT) == (None, None)


def test_numeric_rejects_bool_as_normalized():
    # True is an int in Python; it must not be read as the number 1.
    value, source = numeric_from_field({"originalValue": None, "normalizedValue": True})
    assert value is None and source is None


def test_numeric_from_field_accepts_bare_scalars():
    assert money_to_str(numeric_from_field(1234.5)[0]) == "1234.50"
    assert money_to_str(numeric_from_field("725.30 EUR")[0]) == "725.30"


def test_arm_a_and_arm_b_shapes_compare_equal():
    from core.matching import compare, MatchRule
    arm_a = {"originalValue": "1,234.56"}
    arm_b = {"originalValue": "1,234.56", "normalizedValue": 1234.56}
    assert compare(arm_a, "1234.56", MatchRule.NUMERIC).matched
    assert compare(arm_b, "1234.56", MatchRule.NUMERIC).matched
    # Both arms scored on the SAME field, so an arm difference is a real pipeline difference.
    assert compare(arm_a, "1234.56", MatchRule.NUMERIC).value_source == "originalValue"
    assert compare(arm_b, "1234.56", MatchRule.NUMERIC).value_source == "originalValue"


# --------------------------------------------------------------------------------------
# Merged-address comparison (Q2)
# --------------------------------------------------------------------------------------

from core.normalize import merge_address_components  # noqa: E402


GT_BLOCK = "16424 Timothy Mission\nMarkville, AK 58294 US"


def test_merge_joins_components_in_order():
    got = merge_address_components({
        "address": "16424 Timothy Mission", "city": "Markville",
        "state": "AK", "postal_code": "58294", "country": "US"})
    assert got == "16424 timothy mission, markville, ak, 58294, us"


def test_merge_skips_nulls_and_blanks():
    got = merge_address_components({"address": "1 A St", "city": None,
                                    "state": "  ", "postal_code": "90001", "country": None})
    assert got == "1 a st, 90001"
    assert merge_address_components({"address": None, "city": None}) is None
    assert merge_address_components(None) is None


def test_merge_accepts_a_bare_string():
    assert merge_address_components(GT_BLOCK) == "16424 timothy mission, markville, ak 58294 us"


def test_split_components_match_the_gt_block():
    """The whole point of Q2: a correctly SPLIT address must score as correct."""
    from core.matching import compare, MatchRule
    split = {"address": "16424 Timothy Mission", "city": "Markville",
             "state": "AK", "postal_code": "58294", "country": "US"}
    r = compare(split, GT_BLOCK, MatchRule.ADDRESS)
    assert r.matched and r.exact, r          # punctuation dropped -> exact, not merely close


def test_everything_crammed_into_address_also_matches():
    from core.matching import compare, MatchRule
    lumped = {"address": "16424 Timothy Mission, Markville, AK 58294 US",
              "city": None, "state": None, "postal_code": None, "country": None}
    assert compare(lumped, GT_BLOCK, MatchRule.ADDRESS).matched


def test_a_genuinely_wrong_address_still_fails():
    from core.matching import compare, MatchRule
    wrong = {"address": "999 Nowhere Road", "city": "Springfield",
             "state": "ZZ", "postal_code": "00000", "country": "US"}
    assert not compare(wrong, GT_BLOCK, MatchRule.ADDRESS).matched


def test_missing_postcode_degrades_but_may_still_pass_anls():
    from core.matching import compare, MatchRule
    partial = {"address": "16424 Timothy Mission", "city": "Markville",
               "state": "AK", "postal_code": None, "country": "US"}
    r = compare(partial, GT_BLOCK, MatchRule.ADDRESS)
    assert not r.exact and 0.8 <= r.score < 1.0, r


# --------------------------------------------------------------------------------------
# NOTE -- merged-source containment
# --------------------------------------------------------------------------------------

def test_note_merges_paymentterms_and_memo():
    from core.matching import compare, MatchRule, merge_prediction_sources
    P = "invoiceInfo.noteText"

    # routed entirely to customerMemo
    m = merge_prediction_sources(P, {"invoiceInfo.customerMemo": "This order is shipped through blue dart courier"})
    assert compare(m, "This order is shipped through blue dart courier", MatchRule.TEXT_CONTAINED).matched

    # SPLIT across both fields -- the case neither single target handles
    m = merge_prediction_sources(P, {"invoiceInfo.paymentTerms.raw_text": "All payments to be made in cash.",
                                     "invoiceInfo.customerMemo": "Contact us for queries on these quotations."})
    assert compare(m, "All payments to be made in cash. Contact us for queries on these quotations.",
                   MatchRule.TEXT_CONTAINED).matched


def test_note_tolerates_extra_content_from_conditions():
    """Template11: NOTE -> paymentTerms, CONDITIONS -> customerMemo. The merge carries text the
    GT NOTE does not, and that must NOT be scored as a miss."""
    from core.matching import compare, MatchRule, merge_prediction_sources
    m = merge_prediction_sources("invoiceInfo.noteText", {
        "invoiceInfo.paymentTerms.raw_text": "Total payment due in 14 days.",
        "invoiceInfo.customerMemo": "will be charged if payment is not made within the due date."})
    r = compare(m, "Total payment due in 14 days.", MatchRule.TEXT_CONTAINED)
    assert r.matched and r.score == 1.0 and not r.exact


def test_note_still_fails_when_the_model_missed_it_or_got_it_wrong():
    from core.matching import compare, MatchRule, merge_prediction_sources
    P = "invoiceInfo.noteText"
    assert not compare(merge_prediction_sources(P, {}), "Thank you for your business!",
                       MatchRule.TEXT_CONTAINED).matched
    assert not compare(merge_prediction_sources(P, {"invoiceInfo.customerMemo": "Unrelated sentence"}),
                       "Thank you for your business!", MatchRule.TEXT_CONTAINED).matched


def test_note_component_paths_are_consumed_not_scored():
    from core.matching import load_rule_registry, consumed_paths
    reg = load_rule_registry("schema/invoice_leaf_paths.tsv")
    assert reg["invoiceInfo.noteText"].value == "text_contained"
    assert "invoiceInfo.customerMemo" not in reg
    assert "invoiceInfo.paymentTerms.raw_text" not in reg
    assert "invoiceInfo.customerMemo" in consumed_paths("schema/invoice_leaf_paths.tsv")
