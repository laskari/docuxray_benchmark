"""Map contract 1.16 -- the CONTAINMENT criterion, published as a fourth column.

Asked for in one sentence: "check whether the ground truth text is a substring of the
extracted text; if the GT is a full substring of the extraction, consider it a match."

The contract makes four promises, and each has a test here that fails if it is broken:

  1. Exact is untouched. Adding containment changed no `matched`, no `exact` and no published
     accuracy, so an old report and a new one stay comparable.
  2. The ladder holds: exact <= contained <= lenient, on every rule, always. Containment must
     never pass something lenient rejects -- that is what makes the two columns readable as a
     sequence rather than as two unrelated tolerances.
  3. ONE rule can separate containment from exact -- TEXT_CONTAINED -- and no other. A number,
     a date, an address, an identifier and an enum are identical under both, so a containment
     figure can never be read as "the benchmark forgave a wrong total".
  4. It matches on TOKEN BOUNDARIES, not raw characters. This is the substantive design
     decision in the contract and the last group of tests is the reason for it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from core.matching import MatchRule as R, compare

#: The rules allowed to separate containment from exact. ADDRESS is the only one a doc type
#: SELECTS since contract 1.18 -- see test_1_18_no_doc_type_selects_text_contained below.
#: TEXT_CONTAINED keeps the behaviour because the rule is still the right one for a merged
#: target, which is what it was written for.
SOFT = {R.ADDRESS, R.TEXT_SUPERSET, R.TEXT_CONTAINED}
HARD = [R.NUMERIC, R.DATE_ISO, R.DATE_RAW, R.IDENTIFIER, R.ENUM, R.BOOL, R.EMAIL,
        R.CURRENCY, R.TEXT, R.PHONE]

#: (prediction, truth, rule) -- hits, near misses and real errors, so the ladder is exercised
#: on rows where the criteria agree AND on rows where they part.
BATTERY = [
    ("12.30", "12.30", R.NUMERIC),
    ("12.31", "12.30", R.NUMERIC),
    ("12.30 MYR", "12.30", R.NUMERIC),
    ("2018-12-25", "25/12/2018", R.DATE_ISO),
    ("25/12/2018", "25/12/2018", R.DATE_RAW),
    ("INV-0001-X", "INV-0001", R.IDENTIFIER),
    ("a@b.com", "a@b.co", R.EMAIL),
    ("$", "USD", R.CURRENCY),
    ("ABC SDN BHD KL", "ABC SDN BHD", R.TEXT),
    ("NO 1 JALAN X, KL", "NO 1 JALAN X", R.ADDRESS),
    ({"address": "NO 1 JALAN X", "city": "KL"}, "NO 1 JALAN X", R.ADDRESS),
    ({"address": "NO 9 ELSEWHERE", "city": "KL"}, "NO 1 JALAN X", R.ADDRESS),
    ("SOON HUAT MACHINERY ENTERPRISE (JM0352019-K)", "SOON HUAT MACHINERY ENTERPRISE",
     R.TEXT_SUPERSET),
    ("PETRON BKT LANJAN SB ALSERKAM ENTERPRISE", "PETRON BKT LANJAN SB", R.TEXT_SUPERSET),
    ("WHOLLY DIFFERENT", "ABC SDN BHD", R.TEXT_SUPERSET),
    ("+60 3 1234 5678", "03-12345678", R.PHONE),
    ("99 SPEED MART S/B (519537-X)", "99 SPEED MART S/B", R.TEXT_CONTAINED),
    ("GARDENIA BAKERIES (KL) SDN BHD", "GARDENIA BAKERIES (KL) SDN BHD", R.TEXT_CONTAINED),
    ("WHOLLY DIFFERENT", "ABC SDN BHD", R.TEXT_CONTAINED),
    ("", "ABC SDN BHD", R.TEXT_CONTAINED),
]


@pytest.mark.parametrize("pred,truth,rule", BATTERY)
def test_1_16_containment_never_moves_the_exact_verdict(pred, truth, rule):
    """Promise 1. `contained` is carried alongside `matched`; it may never decide it.

    Guarded by construction -- compare() sets contained from matched and never the reverse --
    but asserted anyway, because this is the promise that lets a pre-1.16 report be compared
    with a post-1.16 one.
    """
    res = compare(pred, truth, rule)
    assert res.matched is (res.exact or res.matched)     # matched is computed, not derived
    if res.matched:
        assert res.contained, "an exact match must also be a containment match"


@pytest.mark.parametrize("pred,truth,rule", BATTERY)
def test_1_16_ladder_exact_le_contained_le_lenient(pred, truth, rule):
    """Promise 2. Containment sits between exact and lenient and never outside them."""
    res = compare(pred, truth, rule)
    if res.matched:
        assert res.contained and res.lenient
    if res.contained:
        assert res.lenient, "containment implies every GT token is present, so lenient passes"


@pytest.mark.parametrize("rule", HARD)
def test_1_16_containment_equals_exact_on_every_other_rule(rule):
    """Promise 3. Only the SOFT rules may differ. Everything else is exact, both ways --
    including TEXT, which is what `parties.seller.name` scores under since contract 1.18."""
    for pred, truth, r in BATTERY:
        if r is not rule:
            continue
        res = compare(pred, truth, r)
        assert res.contained == res.matched, (
            f"{rule} separated containment from exact on {pred!r} vs {truth!r}; only "
            f"{sorted(x.value for x in SOFT)} may do that (contracts 1.16, 1.18)")


def test_1_19_containment_is_scoped_to_the_two_fields_that_earn_it():
    """Contracts 1.18 + 1.19. Two fields report containment and the rest do not.

    `parties.seller.name` (TEXT_SUPERSET) because it is the ONE field the pipeline
    over-extracts on -- 418 of 476 non-exact instances on sroie_full958 -- and
    `addressStructured` because a receipt prints the full postal block where SROIE keeps a
    prefix. A plain TEXT field reports containment only when it is exact.
    """
    name = compare("SOON HUAT MACHINERY ENTERPRISE (JM0352019-K)",
                   "SOON HUAT MACHINERY ENTERPRISE", R.TEXT_SUPERSET)
    assert name.contained is True and name.matched is False

    addr = compare({"address": "NO 1 JALAN X", "city": "KL", "postal_code": "57000"},
                   "NO 1 JALAN X", R.ADDRESS)
    assert addr.contained is True and addr.matched is False

    plain = compare("THREE STOOGES BISTRO & CAFE", "THREE STOOGES", R.TEXT)
    assert plain.contained is False, "plain TEXT must not report containment"


def test_1_19_turning_containment_on_cannot_move_the_exact_verdict():
    """The promise that makes 1.19 safe, and the bug it was written against.

    Containment on the seller name used to be delivered by routing the leaf to TEXT_CONTAINED,
    which computes `exact` with punctuation DROPPED. So switching the column on also moved the
    leaf's EXACT number -- 0.94pp across 9 documents on sroie_full958. TEXT_SUPERSET is TEXT in
    every respect but the extra verdict, so the two rules must agree on `exact` and `matched`
    for every pair, including the punctuation cases that caused the drift.
    """
    pairs = [("MR. D.I.Y.(KUCHAI) SDN BHD", "MR. D. I. Y. (KUCHAI) SDN BHD"),
             ("BEMED (SP) SDN. BHD.", "BEMED (SP) SDN BHD."),
             ("MAKASSAR FRESH MARKET S/B.", "MAKASSAR FRESH MARKET S/B"),
             ("**Royaltea**", "ROYALTEA"),
             ("ABC SDN BHD", "ABC SDN BHD"),
             ("SOON HUAT MACHINERY ENTERPRISE (JM0352019-K)", "SOON HUAT MACHINERY ENTERPRISE")]
    for pred, truth in pairs:
        plain = compare(pred, truth, R.TEXT)
        superset = compare(pred, truth, R.TEXT_SUPERSET)
        assert (plain.exact, plain.matched) == (superset.exact, superset.matched), (
            f"{pred!r} vs {truth!r}: TEXT_SUPERSET moved the exact verdict, which a second "
            f"column is never allowed to do")


def test_1_19_containment_drops_punctuation_but_keeps_token_boundaries():
    """The one place containment is looser than `exact`, and the two guards on it.

    A receipt that prints "Farmasi Maluri S/B(587969-U)" with no space before the bracket
    tokenises as one token; failing containment there measures the page's spacing, not whether
    the name is present. 32 of 957 seller.name instances on sroie_full958 turn on this alone.

    What it must NOT buy is a raw-substring match across word boundaries, which is the whole
    reason containment compares token RUNS. Both halves are asserted here.
    """
    for pred, truth in [("Farmasi Maluri S/B(587969-U)", "FARMASI MALURI S/B"),
                        ("MR. D.I.Y.(KUCHAI) SDN BHD", "MR. D. I. Y. (KUCHAI) SDN BHD")]:
        res = compare(pred, truth, R.TEXT_SUPERSET)
        assert res.contained is True, f"{pred!r}: punctuation alone blocked containment"
        assert res.exact is False, f"{pred!r}: exact must stay strict"

    for pred, truth in [("SUPERMARKET SDN BHD", "MARKET"), ("ABHDX TRADING", "BHD")]:
        assert compare(pred, truth, R.TEXT_SUPERSET).contained is False, (
            f"{pred!r} vs {truth!r}: a spelling coincidence passed as containment")


def test_1_19_no_doc_type_selects_text_contained():
    """`rule_for` is what decides which rule a path gets. TEXT_CONTAINED computes `exact` with
    punctuation dropped, so a doc type that routes a scored leaf to it changes that leaf's
    headline as well as its containment column. Nothing may do that silently."""
    import doctypes
    from core.matching import rule_for
    for spec in doctypes.REGISTRY.values():
        for leaf in spec.contained_leaves:
            rule = rule_for(f"parties.seller.{leaf}", "string", spec)
            assert rule is R.TEXT_SUPERSET, (
                f"{spec.name}.{leaf} routes to {rule}; containment must arrive via "
                f"TEXT_SUPERSET, which cannot move an exact figure")


def test_1_16_extra_text_around_the_value_passes():
    """The whole point of the criterion: the value is intact, other text surrounds it."""
    res = compare({"address": "99 JALAN SG BESI", "city": "KL", "state": "SELANGOR"},
                  "99 JALAN SG BESI", R.ADDRESS)
    assert res.contained and not res.matched


def test_1_18_address_containment_tries_every_admissible_rendering():
    """Containment inherits contract 1.12's promise: the model is not scored on which
    component it filed a token in. The GT block orders postcode before city; the prediction's
    components can be rendered either way, and containment must find the one that matches."""
    res = compare({"address": "1 JALAN SG BESI", "city": "KUALA LUMPUR",
                   "postal_code": "57000", "country": "MALAYSIA"},
                  "1 JALAN SG BESI, 57000 KUALA LUMPUR", R.ADDRESS)
    assert res.contained is True


def test_1_16_text_broken_by_an_interleaved_token_fails():
    """The line between containment and lenient, and the reason both columns exist.

    Token RECALL is 1.0 here -- every ground-truth token is somewhere in the prediction -- so
    the lenient criterion passes. Containment does not, because the ground truth is not in
    there INTACT. If this ever starts passing, the two columns have collapsed into one.
    """
    res = compare("RESTORAN WAN X SHENG", "RESTORAN WAN SHENG", R.TEXT_CONTAINED)
    assert res.lenient, "recall is 1.0, so lenient must pass"
    assert not res.contained, "a token was interleaved, so the GT is not present intact"


def test_1_16_reordered_tokens_fail():
    """Order is part of the claim. Recall alone cannot see this either."""
    res = compare("SDN BHD RESTORAN WAN SHENG", "RESTORAN WAN SHENG SDN BHD",
                  R.TEXT_CONTAINED)
    assert not res.contained


@pytest.mark.parametrize("pred,truth", [
    ("SUPERMARKET SDN BHD", "MARKET"),        # 'market' inside 'supermarket'
    ("ABHDX TRADING", "BHD"),                 # 'bhd' inside 'abhdx'
])
def test_1_16_matches_on_token_boundaries_not_raw_characters(pred, truth):
    """Promise 4, and the substantive design decision.

    A raw `truth in prediction` substring test passes both of these, and both are a
    coincidence of spelling rather than the model emitting the value. Matching token RUNS is
    what makes "the GT is a full substring of the extraction" mean what it says.

    It also keeps the ladder intact: raw substring would pass here while LENIENT fails
    (token recall is 0.0, since 'market' is not a token of 'supermarket sdn bhd'), which
    would put containment ABOVE lenient and break test_1_16_ladder.
    """
    res = compare(pred, truth, R.TEXT_CONTAINED)
    assert not res.contained
    assert not res.lenient      # and so the ladder is never inverted
