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


def test_numeric_from_field_prefers_normalized_by_default():
    # normalizedValue is the default source (changed 2026-09-04). It is production's own parse
    # of the printed string, and it is the only key that survives the postprocessor's sign
    # rewrite on discountTotal.
    value, source = numeric_from_field({"originalValue": "1,234.56", "normalizedValue": 1234.56})
    assert money_to_str(value) == "1234.56"
    assert source == "normalizedValue"


def test_numeric_from_field_can_prefer_original_when_asked():
    value, source = numeric_from_field({"originalValue": "1,234.56", "normalizedValue": 1234.56},
                                       prefer="originalValue")
    assert money_to_str(value) == "1234.56"
    assert source == "originalValue"


def test_discount_sign_rewrite_is_read_correctly():
    """THE regression this change exists for.

    ai/postprocessing/invoice_postprocessor.py rewrites discountTotal's originalValue from
    "9.93" to "(-) 9.93" while setting normalizedValue to 9.93. FATURA's ground truth stores
    discount as a positive magnitude, so reading originalValue scored FINAL 0/13 on a field
    RAW scored 12/13 -- a fabricated 100% regression. Measured on the 51-document probe,
    this is the ONLY path where the two keys disagree.
    """
    final = {"originalValue": "(-) 9.93", "normalizedValue": 9.93}
    value, source = numeric_from_field(final)
    assert money_to_str(value) == "9.93", "the sign rewrite leaked back in"
    assert source == "normalizedValue"


def test_raw_arm_still_reads_when_normalized_does_not_exist():
    """The old objection to normalizedValue was that RAW would score zero. It does not:
    the model emits only originalValue there, and the fallback picks it up."""
    value, source = numeric_from_field({"originalValue": "1,234.56"})
    assert money_to_str(value) == "1234.56"
    assert source == "originalValue"


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


def test_a_present_but_null_normalized_is_the_answer_not_a_gap():
    """Changed 2026-09-04, and it is the most consequential rule in this module.

    A normalizedValue that EXISTS and holds null is production saying "I computed a value and
    rejected it" -- and null is what the customer receives. Rescuing the field from
    originalValue credits the model for something nobody ever sees. On the 51-document probe
    that scored totals.taxPercentage at 100% in FINAL while 16 documents shipped null:
    refinement rewrites the field to "(4.65%)", _normalize_numeric_value reads the parentheses
    as negation, -4.65 fails the [0,100] check, and the field is nulled.
    """
    value, source = numeric_from_field({"originalValue": "725.30 EUR", "normalizedValue": None})
    assert value is None, "a shipped null must not be rescued from originalValue"
    assert source == "normalizedValue", "source names the key that decided the outcome"


def test_a_missing_key_still_falls_back():
    """The other half of the rule, and why the RAW arm is not zeroed: the model cannot emit
    normalizedValue at all (new_schema.NumericValue sets extra='forbid'), so an ABSENT key
    means the stage has decided nothing and the other key is read."""
    value, source = numeric_from_field({"originalValue": "725.30 EUR"})
    assert money_to_str(value) == "725.30"
    assert source == "originalValue"


def test_numeric_from_field_null_wrapper():
    # Both keys present and null: still no value; the preferred key names the decision.
    assert numeric_from_field({"originalValue": None, "normalizedValue": None}) \
        == (None, "normalizedValue")
    assert numeric_from_field(None) == (None, None)
    assert numeric_from_field(ABSENT) == (None, None)


def test_numeric_rejects_bool_as_normalized():
    # True is an int in Python; it must never read as the number 1. A non-numeric
    # normalizedValue is malformed rather than a decision, so the fallback still applies --
    # and here originalValue is itself null, so the answer is still no value.
    value, source = numeric_from_field({"originalValue": None, "normalizedValue": True})
    assert value is None
    assert source == "originalValue"


def test_numeric_from_field_accepts_bare_scalars():
    assert money_to_str(numeric_from_field(1234.5)[0]) == "1234.50"
    assert money_to_str(numeric_from_field("725.30 EUR")[0]) == "725.30"


def test_both_arm_shapes_compare_equal():
    """The two arms carry different wire shapes and must still score the same fact.

    They are read from DIFFERENT keys -- RAW has no normalizedValue -- so value_source is
    published per row precisely so an arm difference can be checked against which key was
    used, rather than assumed to be a model difference.
    """
    from core.matching import compare, MatchRule
    raw = {"originalValue": "1,234.56"}
    final = {"originalValue": "1,234.56", "normalizedValue": 1234.56}
    assert compare(raw, "1234.56", MatchRule.NUMERIC).matched
    assert compare(final, "1234.56", MatchRule.NUMERIC).matched
    assert compare(raw, "1234.56", MatchRule.NUMERIC).value_source == "originalValue"
    assert compare(final, "1234.56", MatchRule.NUMERIC).value_source == "normalizedValue"


# --------------------------------------------------------------------------------------
# Merged-address comparison (Q2)
# --------------------------------------------------------------------------------------

from core.normalize import merge_address_components  # noqa: E402


GT_BLOCK = "16424 Timothy Mission\nMarkville, AK 58294 US"


def test_merge_joins_components_in_order():
    got = merge_address_components({
        "address": "16424 Timothy Mission", "city": "Markville",
        "state": "AK", "postal_code": "58294", "country": "US"})
    assert got == "16424 timothy mission markville ak 58294 us"


def test_merge_skips_nulls_and_blanks():
    got = merge_address_components({"address": "1 A St", "city": None,
                                    "state": "  ", "postal_code": "90001", "country": None})
    assert got == "1 a st 90001"
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
# is_emitted -- structural presence is not an emission
# --------------------------------------------------------------------------------------

from core.normalize import is_emitted  # noqa: E402


def test_empty_shells_are_not_emissions():
    """The extraction schema is a fixed Pydantic tree, so a field the model declined to answer
    comes back as the SHAPE of an answer with nothing in it. Against a GT that says ABSENT
    those are correct nulls, not hallucinations.

    The predicate this replaced tested `str(pred).strip() != ""`, and `str({})` is "{}" --
    non-empty -- so on the 51-document probe 9 correctly-empty seller addresses scored as
    9 hallucinations in BOTH arms.
    """
    assert not is_emitted(None)
    assert not is_emitted("")
    assert not is_emitted("   ")
    assert not is_emitted({})
    assert not is_emitted([])
    assert not is_emitted(ABSENT)
    assert not is_emitted({"originalValue": None})
    assert not is_emitted({"originalValue": None, "normalizedValue": None})
    assert not is_emitted({"address": None, "city": None, "state": None,
                           "postal_code": None, "country": None})
    assert not is_emitted({"a": {"b": None}}), "must recurse, not just check the top level"


def test_real_values_are_emissions():
    assert is_emitted("Acme Ltd")
    assert is_emitted({"originalValue": "9.93"})
    assert is_emitted({"address": None, "city": "Markville"})
    assert is_emitted({"a": {"b": "v"}})


def test_zero_and_false_are_emissions():
    """A real zero is an answer. Folding it into 'absent' would silently forgive a model that
    emits 0.00 for a total it could not read."""
    assert is_emitted(0)
    assert is_emitted(0.0)
    assert is_emitted(False)
    assert is_emitted({"originalValue": "0.00", "normalizedValue": 0.0})
