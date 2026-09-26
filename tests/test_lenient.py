"""Map contract 1.17 -- the LENIENT criterion, published beside the exact one.

1.17 REPLACES 1.14's similarity bar. Lenient is no longer "ANLS >= 0.8"; it is exact equality
after both sides shed ONE named piece of printed scaffolding -- the business-registration
number a receipt prints beside the trading name, written as any parenthesised group containing
a digit. ANLS now decides nothing on any rule and `--anls-threshold` is inert.

The contract makes four promises, and each has a test here that fails if it is broken:

  1. Exact is untouched. Changing lenient changed no `matched`, no `exact` and no published
     accuracy. This is the promise that lets an old report and a new one be compared.
  2. The verdicts are a LADDER: exact <= convention <= lenient and exact <= contained <=
     lenient, on every rule, always. Without it the gap between two columns stops being
     attributable to one rule.
  3. Lenient loosens the store-number rule and NOTHING ELSE. A number, a date, an identifier,
     an enum, an email and a charge label score identically under both criteria -- so
     "lenient" can never be read as "the benchmark forgave a wrong total".
  4. The rule removes a registration number and never a word of the name itself. It resolves
     261 seller.name mismatches on sroie_full958 and breaks ZERO exact matches; the second
     number is the one that makes it a rung of a ladder rather than a separate tolerance.

The fourth group of tests is the one worth reading before quoting a lenient figure: what the
criterion CANNOT see.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from core import matching as M
from core.matching import MatchRule as R, compare


# The rules that lenient is allowed to separate from exact. Everything else must not move.
# TEXT and ADDRESS separate via the store-number strip; PHONE via its digits-only reading;
# TEXT_CONTAINED via token recall. No doc type selects TEXT_CONTAINED since contract 1.18
# (see test_containment.py) -- it stays in this set because the RULE still behaves that way
# and a doc type could select it again for a merged target.
SOFT = {R.TEXT, R.TEXT_SUPERSET, R.ADDRESS, R.TEXT_CONTAINED, R.PHONE}
HARD = [R.NUMERIC, R.DATE_ISO, R.DATE_RAW, R.IDENTIFIER, R.ENUM, R.BOOL, R.EMAIL,
        R.CURRENCY, R.CHARGE_LABEL]

#: (prediction, truth, rule). Deliberately a mix of hits, near misses and real errors, so the
#: ladder is exercised on rows where the criteria agree AND on rows where they part.
BATTERY = [
    ("12.30", "12.30", R.NUMERIC),
    ("12.31", "12.30", R.NUMERIC),
    ("2018-12-25", "25/12/2018", R.DATE_ISO),
    ("2018-12-24", "25/12/2018", R.DATE_ISO),
    ("25/12/2018", "25/12/2018", R.DATE_RAW),
    ("INV-001", "INV-0001", R.IDENTIFIER),
    ("a@b.com", "a@b.co", R.EMAIL),
    ("$", "USD", R.CURRENCY),
    ("TAX:VAT (5.99%)", "VAT(5.99%)", R.CHARGE_LABEL),
    ("ABC SDN BHD", "ABC SDN. BHD.", R.TEXT),
    ("WHOLLY DIFFERENT", "ABC SDN BHD", R.TEXT),
    ("99 SPEED MART S/B 519537-X", "99 SPEED MART S/B", R.TEXT_CONTAINED),
    ("+(352)259-8443", "+352 259 8443", R.PHONE),
    ({"address": "1 JALAN SG BESI", "city": "KL", "postal_code": "57000"},
     "1 JALAN SG BESI, 57000 KL", R.ADDRESS),
    ({"address": "1 JALAN SG BESI", "city": "KL", "postal_code": "57001"},
     "1 JALAN SG BESI, 57000 KL", R.ADDRESS),
]


# ---------------------------------------------------------------------------------------
# 2. the ladder
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("pred,truth,rule", BATTERY)
def test_1_14_the_three_verdicts_are_a_ladder(pred, truth, rule):
    """exact <= convention <= lenient, with no exception anywhere.

    Enforced in compare() rather than in each branch, so this also guards a rule added later:
    a new branch that forgets to set `lenient` inherits the exact verdict and still passes,
    and one that sets it WRONG (lower than exact) fails here.
    """
    res = compare(pred, truth, rule)
    assert not res.matched or res.convention, f"{rule}: exact passed but convention did not"
    assert not res.convention or res.lenient, f"{rule}: convention passed but lenient did not"


# ---------------------------------------------------------------------------------------
# 3. what lenient may and may not loosen
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("pred,truth,rule", [b for b in BATTERY if b[2] in HARD])
def test_1_14_hard_rules_score_identically_under_both_criteria(pred, truth, rule):
    """No tolerance reaches a number, a date, an identifier, an enum, an email or a charge
    label. CURRENCY may differ from EXACT via the contract-1.11 convention list, which is a
    closed enumeration and not a threshold -- so lenient is tested against `convention` there,
    which is the verdict that already admits '$' for USD."""
    res = compare(pred, truth, rule)
    floor = res.convention if rule is R.CURRENCY else res.matched
    assert res.lenient == floor, (
        f"{rule} separated lenient from exact; only {sorted(r.value for r in SOFT)} may")


def test_1_14_a_wrong_total_is_wrong_under_both_criteria():
    """The single most important thing the lenient column must NOT do. 12.31 for a printed
    12.30 is a one-cent error on an invoice total and fails both readings."""
    res = compare("12.31", "12.30", R.NUMERIC)
    assert (res.matched, res.lenient) == (False, False)


def test_1_14_a_wrong_date_is_wrong_under_both_criteria():
    res = compare("2018-12-24", "25/12/2018", R.DATE_ISO)
    assert (res.matched, res.lenient) == (False, False)


def test_1_14_a_transposed_identifier_is_wrong_under_both_criteria():
    """An ID is an identity: 'INV-001' against 'INV-0001' is ANLS 0.875, comfortably over the
    0.8 bar, and would pass if IDENTIFIER were ever routed through the soft path."""
    res = compare("INV-001", "INV-0001", R.IDENTIFIER)
    assert (res.matched, res.lenient) == (False, False)


@pytest.mark.parametrize("pred,truth,rule,why", [
    ("SWC ENTERPRISE SDN BHD (1125830-V)", "SWC ENTERPRISE SDN BHD", R.TEXT,
     "a bare registration number after the name"),
    ("KEDAI BUKU NEW ACHEIVERS (CO NO. SA0345444-K)", "KEDAI BUKU NEW ACHEIVERS", R.TEXT,
     "a LABELLED registration number -- the variant the narrow regex missed"),
    ("GARDENIA BAKERIES (KL) SDN BHD (139386 X)", "GARDENIA BAKERIES (KL) SDN BHD", R.TEXT,
     "a space-separated number, with a digit-free parenthetical that must survive"),
    ({"address": "1 JALAN SG BESI (12345-A)", "city": "KL"}, "1 JALAN SG BESI, KL", R.ADDRESS,
     "a registration number that leaked into the address block"),
    ("99 SPEED MART S/B 519537-X", "99 SPEED MART S/B", R.TEXT_CONTAINED,
     "the registration number appended to the name"),
    ("+(352)259-8443", "+352 259 8443", R.PHONE, "the printed punctuation of a phone number"),
])
def test_1_17_the_soft_rules_do_separate(pred, truth, rule, why):
    """Each soft rule is exercised on a case it is MEANT to separate, so a future change that
    silently collapses lenient onto exact -- raising the threshold to 1.0, say -- is caught
    here rather than discovered when the two columns print the same number."""
    res = compare(pred, truth, rule)
    assert not res.matched, f"{rule}: expected the exact criterion to fail on {why}"
    assert res.lenient, f"{rule}: expected the lenient criterion to pass on {why}"


# ---------------------------------------------------------------------------------------
# 4. what the lenient number CANNOT see -- read this before quoting it
# ---------------------------------------------------------------------------------------
def test_1_17_lenient_does_not_forgive_a_different_trading_name():
    """The substantive tightening in 1.17/1.18, and the reason the old numbers moved.

    Under 1.14 `parties.seller.name` carried TEXT_CONTAINED, so a prediction that CONTAINED the
    annotated name passed however much else it said: "THREE STOOGES BISTRO & CAFE" scored as a
    match for a ground truth of "THREE STOOGES", and so did a prediction that glued two
    companies together. That is a different trading name, not the same one with scaffolding
    around it. Under the store-number rule both fail, because neither difference is a
    parenthesised number.
    """
    for pred in ("THREE STOOGES BISTRO & CAFE",
                 "PETRON BKT LANJAN SB ALSERKAM ENTERPRISE",
                 "UNIHAKKA INTERNATIONAL SDN BHD BAR WANG RICE@PERMAS JAYA"):
        res = compare(pred, pred.split()[0] + " " + pred.split()[1], R.TEXT)
        assert res.lenient is False or res.matched, (
            f"{pred!r}: lenient forgave extra words that are not a registration number")


def test_1_17_the_rule_removes_a_number_and_never_a_word_of_the_name():
    """Promise 4, the half that protects the ladder: a digit-free parenthetical is part of the
    name and the rule cannot reach it. These six are real corpus values -- if the regex ever
    widens to "any parenthetical", every one of them silently becomes a match against a
    DIFFERENT company and the lenient column stops meaning anything."""
    for name in ("AEON CO. (M) BHD",
                 "GARDENIA BAKERIES (KL) SDN BHD",
                 "THE STORE (MALAYSIA) SDN BHD",
                 "BOOK TA .K (TAMAN DAYA) SDN BHD"):
        assert M.strip_store_number(name) == name, f"{name!r}: the rule ate part of the name"
    assert M.strip_store_number("SWC ENTERPRISE SDN BHD (1125830-V)") == "SWC ENTERPRISE SDN BHD"
    assert M.strip_store_number("MR. D.I.Y. SDN BHD (CO.REG :704427-T )") == "MR. D.I.Y. SDN BHD"


def test_1_17_the_rule_cannot_turn_an_exact_match_into_a_miss():
    """Measured over sroie_full958 as 'breaks zero exact matches'; asserted here on the shapes
    that would break it. Applying the strip to BOTH sides is what makes this hold -- strip one
    side only and a ground truth that itself carries a registration number stops matching."""
    for value in ("AEON CO. (M) BHD (126926-H)", "SWC ENTERPRISE SDN BHD (1125830-V)",
                  "GARDENIA BAKERIES (KL) SDN BHD", "99 SPEED MART S/B"):
        res = compare(value, value, R.TEXT)
        assert (res.matched, res.lenient) == (True, True), f"{value!r} stopped matching itself"


def test_1_17_lenient_still_fails_a_misread_street():
    """The reassurance on the other side: leniency is a named removal, not a bar. A block whose
    city is simply wrong fails all three criteria -- and unlike 1.14 it now fails regardless of
    how long the block is, because no similarity score is consulted."""
    res = compare({"address": "12 JALAN PERSIARAN BUKIT BINTANG", "city": "JOHOR"},
                  "12 JALAN PERSIARAN BUKIT BINTANG, KUALA LUMPUR", R.ADDRESS)
    assert (res.matched, res.contained, res.lenient) == (False, False, False)


def test_1_17_the_address_verdict_is_no_longer_length_dependent():
    """1.14's worst property, removed. ANLS forgave a wrong or extra token in proportion to the
    LENGTH of the block, so the SAME error -- an appended country the page does not print --
    passed on a long address (ANLS 0.877) and failed on a short one (0.727). The lenient
    address figure was therefore partly a statement about how long this corpus's addresses are.

    Under 1.17 both behave the same way, which is the point of the change. Containment carries
    them: the ground-truth block is present intact inside the prediction in both cases.
    """
    long_block = {"address": "12 JALAN PERSIARAN BUKIT BINTANG TAMAN MALURI",
                  "city": "KUALA LUMPUR", "postal_code": "56100", "country": "MALAYSIA"}
    short_block = {"address": "1 JALAN SG BESI", "city": "KL", "postal_code": "57000",
                   "country": "MALAYSIA"}
    long_res = compare(long_block, "12 JALAN PERSIARAN BUKIT BINTANG TAMAN MALURI, "
                                   "56100 KUALA LUMPUR", R.ADDRESS)
    short_res = compare(short_block, "1 JALAN SG BESI, 57000 KL", R.ADDRESS)
    assert long_res.lenient == short_res.lenient is True, (
        "the same appended country must get the same verdict on both blocks")
    assert long_res.matched is short_res.matched is False


def test_1_17_what_the_address_containment_number_cannot_see():
    """Read this before quoting the `contains` column. Containment is asymmetric: it forgives
    arbitrary text on either side of the ground truth, so a prediction that appends a whole
    second address passes. It answers "is the right answer in there, unbroken", never "is this
    the answer" -- which is exactly why exact is published beside it and not replaced by it."""
    res = compare({"address": "1 JALAN SG BESI", "city": "KL",
                   "state": "AND ALSO 99 JALAN SOMEWHERE ELSE IPOH PERAK"},
                  "1 JALAN SG BESI, KL", R.ADDRESS)
    assert res.contained is True and res.matched is False


# ---------------------------------------------------------------------------------------
# 1. exact is untouched
# ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("pred,truth,rule", BATTERY)
def test_1_14_lenient_never_moves_the_exact_verdict(pred, truth, rule):
    """The exact verdict is recomputed from the normalisation primitives directly and must
    agree with what compare() reports. A change to the lenient path that leaked into `matched`
    -- the one regression that would invalidate every published number -- fails here."""
    res = compare(pred, truth, rule)
    assert res.matched == res.exact or rule is R.TEXT_CONTAINED, (
        "under match_policy=exact every rule's headline verdict IS its exact verdict")


def test_1_17_the_anls_threshold_is_inert():
    """Under 1.14 `--anls-threshold` moved the lenient column; under 1.17 it moves NOTHING on
    the rules a doc type selects. Asserted rather than left as a claim in the docs, because
    results.json still records the value and a reader is entitled to know it decided nothing.

    TEXT_CONTAINED is excluded: its lenient reading is token recall at a FIXED 0.9 bar, which
    is not this knob, and no doc type selects that rule anyway (contract 1.18).
    """
    battery = [("ABC SDN BHD", "ABC SDN. BHD.", R.TEXT),
               ("SWC ENTERPRISE SDN BHD (1125830-V)", "SWC ENTERPRISE SDN BHD", R.TEXT),
               ({"address": "1 JALAN SG BESI", "city": "KL"}, "1 JALAN SG BESI, KL", R.ADDRESS),
               ({"address": "9 WRONG STREET", "city": "KL"}, "1 JALAN SG BESI, KL", R.ADDRESS)]
    before = [compare(p, t_, r) for p, t_, r in battery]
    old = M.ANLS_THRESHOLD
    try:
        M.set_anls_threshold(1.0)
        after = [compare(p, t_, r) for p, t_, r in battery]
    finally:
        M.set_anls_threshold(old)
    for b, a, (p, _, rule) in zip(before, after, battery):
        assert (b.matched, b.contained, b.lenient) == (a.matched, a.contained, a.lenient), (
            f"{rule} on {p!r}: raising the ANLS bar to 1.0 moved a verdict")
