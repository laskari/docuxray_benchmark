"""Per-type match predicates.

Match rules are keyed by the field's SCHEMA TYPE, never by its name, so adding a field to
new_schema.py never reopens the benchmark contract. Types come from
schema/invoice_leaf_paths.tsv, which is generated from the Pydantic models.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import (  # noqa: E402
    ABSENT, _Absent, anls, normalise_address, normalise_date, normalise_money,
    merge_address_components, address_renderings, normalise_address_compare, normalise_phone,
    normalise_phone_compare, normalise_string, numeric_from_field,
    numeric_parse_disagreement, token_f1,
)

ANLS_THRESHOLD = 0.8          # the document-AI standard, so string numbers stay comparable

#: The scaffolding a printed charge label carries around its two facts. "TAX:" is a column
#: caption the page repeats on every tax line, not part of the charge's name.
_CHARGE_RATE = re.compile(r"\(\s*([-+]?\d+(?:\.\d+)?)\s*%\s*\)")
_CHARGE_PREFIX = re.compile(r"(?i)^\s*tax\s*[:\-]\s*")


def charge_label_parts(value: Any) -> Tuple[str, Optional[float]]:
    """Split a charge label into (name, rate) so the two facts can be compared separately.

    Map contract 1.10. Returns the name strictly normalised and the rate rounded to 2dp, or
    None when the label states no rate. Exposed rather than private so a review workbook can
    show the same two parts the verdict was taken on.

        "TAX:VAT (5.99%)"  -> ("vat", 5.99)
        "GST(18%) :"       -> ("gst", 18.0)
        "VAT(6.5%)"        -> ("vat", 6.5)
        "GST, VAT(3.33%)"  -> ("gst, vat", 3.33)     # a real miss against ("vat", 3.33)
        "Shipping"         -> ("shipping", None)
    """
    if value is None or isinstance(value, _Absent):
        return "", None
    text = str(value).strip()
    m = _CHARGE_RATE.search(text)
    rate: Optional[float] = None
    if m:
        try:
            rate = round(float(m.group(1)), 2)
        except (TypeError, ValueError):
            rate = None
        text = text[:m.start()] + text[m.end():]
    text = _CHARGE_PREFIX.sub("", text)
    text = text.strip().strip(":-").strip()
    return normalise_string(text), rate



def set_anls_threshold(value: float) -> None:
    """Set the similarity bar the `threshold` policy uses for text and address rules.

    0.8 is the ICDAR/DocVQA convention and the default, which is what makes the lenient number
    comparable to published document-AI work. Raising it narrows the gap to exact; it is a knob
    on ONE policy, and it has no effect at all under `match_policy: exact`.
    """
    global ANLS_THRESHOLD
    if not 0.0 < value <= 1.0:
        raise ValueError(f"anls_threshold must be in (0, 1]; got {value!r}")
    ANLS_THRESHOLD = float(value)
_DATE_FORMATS_PRED = None     # predictions may use any known format; GT is dataset-pinned


class MatchRule(str, Enum):
    NUMERIC = "numeric"           # money / percent / quantity -- 2dp exact, no tolerance
    DATE_ISO = "date_iso"         # parse both to ISO-8601, exact
    DATE_RAW = "date_raw"         # format fidelity only; never in the same denominator as ISO
    IDENTIFIER = "identifier"     # strict-normalised exact -- a near miss is a full miss
    ENUM = "enum"                 # exact; reported separately
    BOOL = "bool"
    TEXT = "text"                 # names, memos -- three tiers reported
    ADDRESS = "address"           # multi-line block -- three tiers reported
    PHONE = "phone"
    EMAIL = "email"
    CURRENCY = "currency"
    TEXT_CONTAINED = "text_contained"   # GT text found across several prediction fields
    TEXT_SUPERSET = "text_superset"     # TEXT, plus a containment reading -- see below
    CHARGE_LABEL = "charge_label"       # a charge row's label: a tax name plus a printed rate


# ---------------------------------------------------------------------------------------
# MATCH POLICY -- one knob, set once, applied to every threshold rule.
# ---------------------------------------------------------------------------------------
# "exact"      a value counts only if it is EXACT after normalisation. One rule for every key,
#              so a single number ("accuracy") means the same thing everywhere. Normalisation
#              is still applied -- NFKC, whitespace collapse, case-fold, punctuation dropped on
#              address blocks -- because comparing raw bytes would measure OCR noise and
#              component-ordering choices rather than whether the value is right.
# "threshold"  the previous behaviour: text and address pass at ANLS >= 0.8 and token recall
#              >= 0.9. Comparable to published DocVQA-style work, but it means the headline
#              number is a different question for different keys, which is what "exact" fixes.
#
# Set from config.yaml `scoring.match_policy`. Two earlier copies of an undocumented
# DOCUXRAY_EXACT_ONLY environment check lived inline in this file and covered only two of the
# three threshold rules; they are replaced by this.
MATCH_POLICY = "exact"


def set_match_policy(name: str) -> None:
    global MATCH_POLICY
    if name not in ("exact", "threshold"):
        raise ValueError(f"unknown match_policy {name!r}; use 'exact' or 'threshold'")
    MATCH_POLICY = name


def _apply_policy(res: "MatchResult") -> "MatchResult":
    """Under the exact policy a threshold pass is demoted to a miss, and the score is kept so
    the report can still show HOW close it was."""
    import os
    policy = MATCH_POLICY
    if os.environ.get("DOCUXRAY_EXACT_ONLY") == "1":
        policy = "exact"                      # kept as a one-off override for ad-hoc runs
    if policy == "exact" and res.matched and not res.exact:
        res.matched = False
        res.reason = res.reason or f"not exact (score {res.score:.3f})"
    return res


# ---------------------------------------------------------------------------------------
# THE LENIENT CRITERION -- map contract 1.14 (Naveen, 2026-09-18)
# ---------------------------------------------------------------------------------------
# A THIRD number, published beside the other two on the same denominators, because "exact"
# and "lenient" answer different questions and a single figure cannot carry both:
#
#   exact       Did the output reproduce the value as the document states it?
#   lenient     Did the output identify the right value, allowing the tolerance published
#               document-AI work uses for free text (ANLS >= 0.8, token recall >= 0.9)?
#
# Exact is what the benchmark publishes and nothing here changes it. `matched`, `exact`,
# every per-field accuracy and every interval are computed from the same code as before; the
# lenient verdict is carried alongside and aggregated into its own keys. A run scored before
# this contract and rescored after it must produce byte-identical exact numbers, and
# tests/test_matching.py::test_1_14_lenient_never_moves_the_exact_verdict checks that.
#
# WHAT LENIENT ADMITS, rule by rule. Four rules can separate it from exact; all others set
# lenient == exact, so a number, a date, an identifier, an enum, an email and a charge label
# score identically under both criteria and no tolerance reaches them.
#
#   TEXT             ANLS >= 0.8 on the normalised strings.
#   ADDRESS          ANLS >= 0.8 on the CLOSEST admissible rendering (contract 1.12).
#   TEXT_CONTAINED   token recall of the GT >= 0.9 within the merged prediction.
#   PHONE            digits only, with a leading '+' (normalise_phone) rather than the
#                    printed form (normalise_phone_compare).
#
# WHAT IT COSTS, stated because the lenient number is the one that will be quoted:
#
#   * ANLS is a string-similarity score, so it forgives a wrong CHARACTER in proportion to
#     the length of the string. On a long address block a transposed postcode digit or a
#     dropped unit number scores ~0.98 and passes. Lenient therefore cannot answer "is this
#     address correct enough to post a letter to"; exact can.
#   * TEXT_CONTAINED is asymmetric and CANNOT DETECT OVER-EXTRACTION at all. On SROIE's
#     parties.seller.name this is the whole difference: the model appending the printed
#     registration number ("99 SPEED MART S/B" -> "99 speed mart s b 519537 x") fails exact
#     and passes lenient, and 232 of that field's 308 exact failures are of exactly that
#     shape. The lenient figure for seller.name answers "did it READ the name", the exact one
#     answers "did it emit the name AND NOTHING ELSE". Both are worth knowing; neither is a
#     substitute for the other, and quoting one as the model's accuracy without saying which
#     is a misreport.
#
# NOT THE SAME AS `match_policy: threshold`. That knob REPLACES the headline verdict with a
# thresholded one, and since contracts 1.6/1.7 it is inert for every headline-eligible rule
# anyway (TEXT and ADDRESS return before _apply_policy). This contract adds a second column
# and changes no headline. ANLS_THRESHOLD is now read by the lenient verdict on every run, so
# --anls-threshold moves the lenient column and only that column.

# ---------------------------------------------------------------------------------------
# THE CONTAINMENT CRITERION -- map contract 1.16 (Claude draft 2026-09-18, pending sign-off)
# ---------------------------------------------------------------------------------------
# A FOURTH number, asked for directly: "check whether the ground truth text is a substring of
# the extracted text; if the GT is a full substring of the extraction, consider it a match."
#
#   exact       Did the output emit the value and NOTHING ELSE?
#   contained   Did the output emit the value INTACT, possibly with other text around it?
#   lenient     Did the output identify the value, allowing the published tolerance?
#
# WHY IT IS NOT REDUNDANT WITH LENIENT, even though on SROIE it lands on the same figure.
# TEXT_CONTAINED's lenient verdict is token-set RECALL at 0.9. That bar passes a prediction
# that holds 90% of the ground-truth tokens, in any order, anywhere -- so "SDN BHD RESTORAN
# WAN" passes against "RESTORAN WAN SHENG SDN BHD" on recall alone. Containment cannot: it
# requires EVERY ground-truth token, IN ORDER, WITH NOTHING INTERLEAVED. It is therefore a
# strictly stronger claim, and where the two agree that agreement is itself the finding --
# see the measurement in the changelog.
#
# WHAT IT STILL CANNOT SEE. Containment is asymmetric in exactly the way exact is not: it
# forgives arbitrary text on either side of the value. "99 SPEED MART S/B 519537 X" contains
# "99 SPEED MART S/B" and passes; so would a prediction that glued two different companies
# together. It answers "is the right answer in there, unbroken", never "is this the answer".
# Quoting it as the model's accuracy without that sentence is a misreport.
#
# ON TOKEN BOUNDARIES, NOT RAW CHARACTERS, and this is a deliberate choice rather than an
# implementation detail. A raw `truth in prediction` substring test matches ACROSS word
# boundaries: a ground truth of "MART" is a substring of "SUPERMARKET", and "BHD" of "ABHDX".
# Those are not the model emitting the value; they are a coincidence of spelling. Comparing
# token RUNS keeps the promise the sentence "the GT is a full substring of the extraction"
# is actually making. It also preserves the LADDER -- a contiguous token run means every GT
# token is present, so token recall is 1.0 and lenient necessarily passes, whereas raw
# substring can pass while lenient FAILS ("mart" inside "supermarket" has recall 0.0) and
# would have broken exact <= contained <= lenient. Measured on sroie_full958: the two
# readings agree on all 957 instances in all three arms, so the boundary rule costs nothing
# here and protects against the corpus where it would.
#
# Normalisation is the SAME one TEXT_CONTAINED already uses for its other verdicts
# (normalise_string(..., drop_punct=True)), so the criteria differ in the comparison and not
# in what they are comparing.


# ---------------------------------------------------------------------------------------
# THE STORE NUMBER -- what the LENIENT criterion removes, and nothing else.
# ---------------------------------------------------------------------------------------
# Since map contract 1.17 lenient is NOT a similarity bar. It is exact equality after both
# sides shed ONE kind of printed scaffolding: the business-registration number a Malaysian
# receipt prints beside the trading name. ANLS decides nothing anywhere any more.
#
# WHY A REGISTRATION NUMBER IS SCAFFOLDING AND NOT PART OF THE NAME. SROIE's ground truth
# records the trading name ("SWC ENTERPRISE SDN BHD"); the page prints the name followed by
# the registrar's number ("SWC ENTERPRISE SDN BHD (1125830-V)"). Both are on the document and
# the model is right about both. Which of the two the annotator chose to store is a
# convention of the dataset, not a fact about the receipt -- the same reason an amount is
# compared at 2dp rather than as printed and an address after its separators collapse.
#
# THE RULE: a parenthesised group that contains a DIGIT is removed from both sides. That is
# the whole rule, and it is deliberately shaped by what the digit does rather than by the
# registration formats seen so far -- the corpus prints at least six of those:
#
#     (1125830-V)   (JM0352019-K)   (SA0264195-T)      bare, and state-prefixed
#     (CO.REG :704427-T )           (Company No 559208-M)   (CO NO. SA0345444-K)   labelled
#     (139386 X)    (8199K)         (LLP0007299-LGN)   space-separated, unseparated, LLP
#
# A narrower rule keyed on `<digits>-<char>` catches 177 of the 261 mismatches this one
# resolves and misses every labelled and space-separated variant, which is why the digit test
# is the one that shipped.
#
# WHAT IT MUST NOT TOUCH, and does not: a parenthetical that is part of the name carries no
# digit, so the rule cannot reach it --
#
#     AEON CO. (M) BHD      GARDENIA BAKERIES (KL) SDN BHD      THE STORE (MALAYSIA) SDN BHD
#     BOOK TA .K (TAMAN DAYA) SDN BHD
#
# Measured over all 958 documents in all three arms: the rule resolves 261 seller.name
# mismatches and breaks ZERO existing exact matches. That second number is the one that makes
# it publishable -- a lenient criterion that can turn a match into a miss is not a rung of a
# ladder. tests/test_lenient.py holds both as standing checks.
#
# SCOPE: the TEXT and ADDRESS rules, which is seller.name and addressStructured on this
# corpus. No number, date, identifier, enum or charge label consults it, so a lenient figure
# can never mean a wrong total was forgiven.
_STORE_NUMBER_RE = re.compile(r"\(\s*[^()]*\d[^()]*\)")


def strip_store_number(text: Optional[str]) -> Optional[str]:
    """Remove every parenthesised group containing a digit, then collapse the seam.

    Whitespace is collapsed afterwards because the removal leaves a double space where the
    group sat ("ASIA MART  " -> "ASIA MART"), and the caller compares normalised strings.
    """
    if not isinstance(text, str):
        return text
    return re.sub(r"\s+", " ", _STORE_NUMBER_RE.sub(" ", text)).strip()


# ---------------------------------------------------------------------------------------
# TEXT_SUPERSET -- substring matching for the ONE field that over-extracts (contract 1.19)
# ---------------------------------------------------------------------------------------
# Asked for directly: "for which field we are generating extra text/output comparing with GT,
# for that field I want to have substring matching." So the field was MEASURED rather than
# assumed. Every non-exact instance of every scored path on sroie_full958, arm FINAL,
# classified by whether the ground truth is a contiguous token run of the prediction:
#
#     field                               non-exact   GT INSIDE prediction   other
#     parties.seller.name                       476            450  (94.5%)      26
#     parties.seller.addressStructured          160             15  ( 9.4%)     145
#     receiptInfo.txnDate                        12              4                 8
#     receiptInfo.txnDateISO                      5              0                 5
#     totals.totalIncludingTax                   10              1                 9
#
# One field over-extracts, by two orders of magnitude in COUNT. `parties.seller.name` is where
# the pipeline emits the registered name PLUS something else -- the printed registration number,
# the outlet, the brand above the legal entity -- and 450 of its 476 misses are that, not a
# misreading. The last three rows are diagnostic only: DATE_RAW and NUMERIC compute no
# containment verdict, so eligibility is DECLARED per rule and never inferred from this table.
#
# WHY THIS IS A SEPARATE RULE AND NOT A RETURN TO TEXT_CONTAINED. Containment on this leaf used
# to be delivered by routing it to MatchRule.TEXT_CONTAINED, and that rule computes `exact`
# with punctuation DROPPED while TEXT keeps it. So the routing decided two things at once: it
# turned the containment column on, and it quietly moved the leaf's EXACT number by 0.94pp
# across 9 documents ("MR. D.I.Y." against an annotated "MR. D. I. Y."). A criterion must not
# be able to move the headline it is published beside. TEXT_SUPERSET is TEXT in every respect
# -- same normalisation, same `exact`, same store-number lenient -- with one extra verdict
# computed. Turning it on or off cannot change an exact figure, and tests/test_containment.py
# asserts exactly that.
#
# WHAT IT FORGIVES, and it is a lot: containment is asymmetric. "PETRON BKT LANJAN SB ALSERKAM
# ENTERPRISE" contains an annotated "PETRON BKT LANJAN SB" and passes, and so would a
# prediction that glued two companies together. It answers "is the right answer in there,
# unbroken", never "is this the answer". That is why `exact` is published beside it and never
# replaced by it, and why §5.1 of the SROIE report partitions the field rather than quoting one
# number for it.


def _strip_address_store_numbers(value):
    """`strip_store_number` applied through an address's shape.

    An address reaches compare() either as the GT's flat block or as the model's five-component
    object, and the lenient criterion has to remove the registration number from both before
    they are merged -- stripping only after the merge would work too, but the merge is what
    address_renderings() does internally, so the strip has to happen on the way in.
    """
    if isinstance(value, dict):
        return {k: strip_store_number(v) for k, v in value.items()}
    return strip_store_number(value)


def _contains_token_run(pred_tokens: List[str], gt_tokens: List[str]) -> bool:
    """Do the ground-truth tokens appear in the prediction contiguously and in order?"""
    if not gt_tokens:
        return True
    n = len(gt_tokens)
    return any(pred_tokens[i:i + n] == gt_tokens
               for i in range(len(pred_tokens) - n + 1))


_ADDRESS_HINT = re.compile(r"addressStructured(\.address)?$")
_ISO_HINT = re.compile(r"ISO$")


# ---------------------------------------------------------------------------------------
# THE CONVENTION-ADJUSTED CRITERION -- map contract 1.11 (Naveen, 2026-09-11)
# ---------------------------------------------------------------------------------------
# Two criteria are published side by side, because they answer different questions:
#
#   exact                 Does the output match the document?
#   convention-adjusted   Did the pipeline READ the document correctly?
#
# The gap between them is not a tolerance and not a similarity score. It is a CLOSED,
# ENUMERATED list of two cases where the page and the schema state the same fact in different
# notation, each one measured before it was admitted:
#
#   A  a printed currency SYMBOL against the ISO CODE for it            594 instances / arm
#   B  a country written long against the form the page prints          103 / 103 / 99
#
# Nothing else is in this list, and adding to it requires a CHANGELOG entry. In particular it
# does NOT forgive: an email whose domain differs, a transposed digit in a phone number or an
# invoice number, a dropped state code, a misread street, or text the model glued onto a name.
# Those are wrong under both criteria, which is the point -- the criterion exists to SEPARATE
# notation from misreading, not to soften the result.
#
# This is NOT the lenient criterion retired in contract 1.7. That one passed an address at
# ANLS >= 0.9 and would have forgiven a misread token; it was retired for exactly that reason.
# This one forgives two named notations and cannot forgive a misreading of any kind.

#: A printed symbol against the ISO code it stands for. Naveen chose USD specifically rather
#: than "any code using this symbol": every instance in this corpus is `$` -> USD, and the
#: narrower rule is the one that can be stated in a sentence. Note the asymmetry with the
#: GROUND TRUTH rule (1.2), which stores `$` as printed and declines to infer a code -- the
#: ground truth still makes no claim about locale; only this second reading does.
CURRENCY_SYMBOL_CODE = {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP"}

#: A country written long against the form the page prints. FATURA prints `us`; the pipeline
#: emits `USA`. 99 of FINAL's 153 address errors are this one word and nothing else.
COUNTRY_LONG_FORM = {"us": "usa", "gb": "uk"}


def _convention_currency(prediction: Any, truth: Any) -> bool:
    """`$` on the page against `USD` on the wire."""
    t = str(truth or "").strip()
    p = str(prediction or "").strip().upper()
    return bool(t) and CURRENCY_SYMBOL_CODE.get(t) == p


def _convention_address(pred_form: str, truth_form: str) -> bool:
    """True when the merged forms differ ONLY by the country written long.

    Compared as token sequences with the differing country token swapped back, so this can
    never forgive a second difference hiding elsewhere in the block: if anything else differs,
    the sequences still do not match.
    """
    if not pred_form or not truth_form:
        return False                      # nothing emitted, or nothing to compare against
    pt, tt = str(pred_form).split(), str(truth_form).split()
    if len(pt) != len(tt):
        return False
    diff = [i for i, (a, b) in enumerate(zip(pt, tt)) if a != b]
    if len(diff) != 1:
        return False
    i = diff[0]
    return COUNTRY_LONG_FORM.get(tt[i]) == pt[i]


def rule_for(path: str, declared_type: str, spec=None) -> MatchRule:
    """Derive the match rule from the generated schema type plus the doc type's own leaf sets.

    Type-driven, not name-driven: adding a field to new_schema.py gets a rule automatically,
    so a schema change never reopens the benchmark contract.
    """
    spec = _spec(spec)
    leaf = path.rsplit(".", 1)[-1]
    # Keyed on the WHOLE leaf path, not the leaf name. 'key' is the leaf name CORD and DOCILE
    # use for their line-item captions too, and those must keep the text rule they were
    # measured under; only the paths a doc type names here change behaviour (contract 1.10).
    if path in getattr(spec, "charge_label_paths", frozenset()):
        return MatchRule.CHARGE_LABEL
    if declared_type == "numeric":
        return MatchRule.NUMERIC
    if declared_type.startswith("enum:"):
        return MatchRule.ENUM
    if declared_type == "bool":
        return MatchRule.BOOL
    if path == "currency":
        return MatchRule.CURRENCY
    if _ISO_HINT.search(path):
        return MatchRule.DATE_ISO
    if leaf in spec.date_leaves:
        return MatchRule.DATE_RAW
    if _ADDRESS_HINT.search(path):
        return MatchRule.ADDRESS
    if leaf == "phone":
        return MatchRule.PHONE
    if leaf == "email":
        return MatchRule.EMAIL
    if leaf in spec.identifier_leaves:
        return MatchRule.IDENTIFIER
    if hasattr(spec, 'contained_leaves') and leaf in spec.contained_leaves:
        return MatchRule.TEXT_SUPERSET
    return MatchRule.TEXT


@dataclass
class MatchResult:
    matched: bool                 # the headline verdict for this rule
    exact: bool                   # exact-after-normalisation (the harsher, honest number)
    score: float                  # ANLS for text rules, 1.0/0.0 otherwise
    token_f1: Optional[float] = None
    reason: str = ""
    # For numerics: which sub-key the predicted value came from ('normalizedValue' |
    # 'originalValue' | None). Aggregated, the originalValue rate on a postprocessed arm is
    # the postprocessor's number-parsing failure rate.
    value_source: Optional[str] = None
    # True when the field carried both originalValue and normalizedValue and they disagreed at
    # 2dp -- i.e. our parser and production's parser read the same printed string differently.
    # Counted and published, never silently resolved.
    numeric_parse_disagreement: bool = False
    # True when the scored sub-key EXISTED on the wire and held null -- production computed a
    # value, rejected it, and shipped nothing. Distinct from a value we merely failed to parse:
    # this is the pipeline dropping the field, so it scores as MISSING rather than WRONG.
    shipped_null: bool = False
    # The CONVENTION-ADJUSTED verdict: True when the two sides state the same fact and differ
    # only in a REPRESENTATION CONVENTION drawn from the closed list in `convention_match`.
    # Never weaker than `matched` -- an exact match is a convention match too. This is a second
    # reading of the same comparison, not a fallback: `matched` is computed and reported exactly
    # as before, and no threshold is involved anywhere (map contract 1.11).
    convention: bool = False
    # The LENIENT verdict -- map contract 1.14. True when the two sides state the same fact
    # under the document-AI standard tolerance for FREE TEXT, which is a similarity bar and
    # not an enumerated list. Never weaker than `matched` or `convention`, enforced in
    # compare(); the three verdicts are a ladder, exact <= convention <= lenient. Only four
    # rules can separate lenient from exact -- TEXT, ADDRESS, TEXT_CONTAINED, PHONE -- and
    # every other rule sets it equal. See `LENIENT_CRITERION` below for what each one admits
    # and what it costs.
    lenient: bool = False
    # The CONTAINMENT verdict -- map contract 1.16. True when the ground truth appears inside
    # the prediction VERBATIM AND UNBROKEN, on token boundaries. A rung of the same ladder,
    # sitting between exact and lenient: exact <= contained <= lenient, enforced in compare().
    # Only TEXT_CONTAINED can separate it from exact; every other rule sets it equal, so a
    # containment figure can never mean a number, a date or an id was forgiven.
    contained: bool = False

    def __bool__(self) -> bool:
        return self.matched


_MISS = MatchResult(False, False, 0.0, reason="one side empty")


def _both_empty(a, b) -> Optional[MatchResult]:
    a_empty = a is None or isinstance(a, _Absent) or (isinstance(a, str) and not a.strip())
    b_empty = b is None or isinstance(b, _Absent) or (isinstance(b, str) and not b.strip())
    if a_empty and b_empty:
        return MatchResult(True, True, 1.0, reason="both empty")
    if a_empty or b_empty:
        return _MISS
    return None


def compare(prediction: Any, truth: Any, rule: MatchRule) -> MatchResult:
    """Compare one predicted value against one GT value under the given rule.

    Returns all THREE verdicts on one result, each a different question about the same pair:

        matched     EXACT after normalisation -- contract 1.6/1.7. Unchanged and computed
                    exactly as before. This is what is published as "accuracy".
        convention  contract 1.11. Exact, plus a CLOSED ENUMERATED list of notation
                    differences (a currency symbol against its ISO code, a country written
                    long). No threshold anywhere.
        contained   contract 1.16. Exact, plus extra text AROUND a ground truth that is
                    reproduced verbatim and unbroken. No threshold -- see
                    CONTAINMENT_CRITERION.
        lenient     contract 1.14. Exact, plus the document-AI standard tolerance on free
                    text (ANLS >= 0.8, token recall >= 0.9). A THRESHOLD, and therefore a
                    different kind of claim -- see LENIENT_CRITERION.

    The four are a LADDER: exact <= convention <= lenient and exact <= contained <= lenient,
    enforced here rather than trusted to each branch, so a new rule cannot silently report a
    LOWER second or third number by forgetting to set a field, and so the gap between any two
    is always attributable to the one rule that widened it. `convention` and `contained` are
    both above exact and both below lenient, and are not ordered against each other: they
    widen different rules and never the same one.
    """
    res = _compare(prediction, truth, rule)
    if res.matched:
        res.convention = True
        res.contained = True
    if res.matched or res.convention or res.contained:
        res.lenient = True
    return res


def _compare(prediction: Any, truth: Any, rule: MatchRule) -> MatchResult:
    short = _both_empty(prediction, truth)
    if short is not None:
        return short

    if rule is MatchRule.NUMERIC:
        # Both sides go through the stage-agnostic accessor: prefer normalizedValue, fall back
        # to parsing originalValue. Exact at 2dp -- no tolerance. A 1p error on a total is wrong.
        p, p_src = numeric_from_field(prediction)
        t, _ = numeric_from_field(truth)
        disagree = numeric_parse_disagreement(prediction)
        if p is None or t is None:
            # numeric_from_field returns (None, key) when `key` was present and explicitly
            # null, and (None, None) when there was nothing readable at all. The first is the
            # product shipping a null; the second is a parse failure. They are different
            # findings and must not share a state.
            shipped_null = p is None and p_src is not None
            return MatchResult(False, False, 0.0,
                               reason=("shipped null" if shipped_null else "unparseable number"),
                               value_source=p_src, numeric_parse_disagreement=disagree,
                               shipped_null=shipped_null)
        ok = p == t
        return MatchResult(ok, ok, float(ok),
                           reason=("" if ok else f"{p} != {t}"),
                           value_source=p_src,
                           numeric_parse_disagreement=disagree)

    if rule is MatchRule.DATE_ISO:
        p, t = normalise_date(prediction), normalise_date(truth)
        if p is None or t is None:
            return MatchResult(False, False, 0.0, reason="unparseable date")
        ok = p == t
        return MatchResult(ok, ok, float(ok), reason="" if ok else f"{p} != {t}")

    if rule is MatchRule.DATE_RAW:
        # Format fidelity: did we reproduce the printed form? Scored, but reported on its own
        # line -- issueDate and issueDateISO encode ONE fact and must never share a denominator.
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok), reason="format fidelity")

    if rule in (MatchRule.IDENTIFIER, MatchRule.ENUM, MatchRule.BOOL):
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok), convention=ok)

    if rule is MatchRule.CURRENCY:
        # Exact is unchanged: the page prints a symbol, the wire carries a code, and they are
        # different strings. The second verdict records that they name the same currency
        # (contract 1.11, case A) -- 594 instances per arm, every one of them `$` against USD.
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok),
                           convention=ok or _convention_currency(prediction, truth))

    if rule is MatchRule.TEXT_CONTAINED:
        # Asymmetric on purpose: token RECALL of the ground truth within the merged prediction,
        # not similarity. Extra content is not penalised, because CONDITIONS -- an unmapped
        # label whose text is genuinely printed on the page -- legitimately lands in the same
        # two fields. Penalising it would score the model down for extracting something real.
        # The cost of that leniency is that this rule cannot detect over-extraction, which is
        # why NOTE is barred from headlines.
        t_norm = normalise_string(truth, drop_punct=True)
        p_norm = normalise_string(prediction, drop_punct=True)
        if not t_norm:
            return MatchResult(True, True, 1.0, reason="no GT text")
        if not p_norm:
            return MatchResult(False, False, 0.0, reason="model emitted nothing")
        gt_tokens = t_norm.split()
        have = set(p_norm.split())
        recall = sum(1 for tok in gt_tokens if tok in have) / len(gt_tokens)
        res = MatchResult(
            matched=recall >= 0.9,
            exact=(t_norm == p_norm),
            score=recall,
            token_f1=token_f1(p_norm, t_norm),
            reason="" if recall >= 0.9 else f"token recall {recall:.2f}",
            # Captured BEFORE _apply_policy demotes `matched`, because this IS the lenient
            # criterion for this rule: every GT token present somewhere in the prediction.
            # It cannot detect over-extraction -- see LENIENT_CRITERION.
            lenient=recall >= 0.9,
            # Contract 1.16, and the only rule that computes this. Stricter than the line
            # above: not "the tokens are all in there somewhere" but "the ground truth is in
            # there INTACT" -- same order, nothing interleaved. Also captured before
            # _apply_policy, for the same reason. See CONTAINMENT_CRITERION.
            contained=_contains_token_run(p_norm.split(), gt_tokens),
        )
        return _apply_policy(res)

    if rule is MatchRule.PHONE:
        # Whitespace removed, nothing else (Naveen 2026-09-09). The ground truth stores the
        # printed number verbatim (map rule 1.2), so brackets, hyphens and slashes are part of
        # the value and are compared. See normalise_phone_compare for the measured cost.
        ok = normalise_phone_compare(prediction) == normalise_phone_compare(truth)
        # Lenient drops to DIGITS (plus a leading '+'), which is the older rule this one
        # replaced: it answers "did the model read the right number?" where exact answers
        # "did it reproduce what is printed?". Both are real questions about a phone number.
        return MatchResult(ok, ok, float(ok),
                           lenient=normalise_phone(prediction) == normalise_phone(truth))

    if rule is MatchRule.EMAIL:
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok))

    if rule is MatchRule.CHARGE_LABEL:
        # A charge row's label is not one value, it is TWO FACTS printed together: which charge
        # this is, and at what rate. "TAX:VAT (5.99%)" states VAT and 5.99. Map contract 1.10
        # (Naveen 2026-09-11) compares those two facts, each under the rule its own type already
        # has -- the name exactly, the rate as a number at 2dp -- after both sides shed the
        # printed scaffolding that is neither fact: a leading "TAX:" token, a trailing colon,
        # and the spacing around the parenthesis.
        #
        # Why this is not a loosening. The ground truth stores a CONSTRUCTED canonical label
        # ("VAT(5.99%)"); the page prints "TAX:VAT (5.99%) :". Comparing those strings measures
        # which of the two renderings the model chose, which is not the question the benchmark
        # asks -- the same reason an address compares after its separators collapse (1.7) and an
        # amount at 2dp rather than as printed. Measured on this run: 84 of 708 matched rows
        # differ as strings, and every one of the 84 names the right tax at the right rate.
        # The rule still fails what should fail -- a wrong tax name at a right rate is a miss,
        # and one document (a comma-joined "GST, VAT") is exactly that.
        #
        # This is EXACT-after-normalisation on both sides, like every other rule here. No
        # similarity score is consulted -- see the note above MatchRule.TEXT.
        p_name, p_rate = charge_label_parts(prediction)
        t_name, t_rate = charge_label_parts(truth)
        ok = (p_name == t_name) and (p_rate == t_rate)
        reason = ""
        if not ok:
            if p_name != t_name:
                reason = f"charge name {p_name!r} != {t_name!r}"
            else:
                reason = f"rate {p_rate} != {t_rate}"
        return MatchResult(ok, ok, float(ok), reason=reason)

    # TEXT and ADDRESS are both compared EXACTLY, and no similarity threshold can loosen either
    # (Naveen 2026-09-09, contracts 1.6 and 1.7). The ANLS score is still computed and carried on
    # the result so a report can show HOW CLOSE a miss was; it does not decide the verdict.
    #
    #   TEXT      the three party names and the tax name. A name is an identity, so a near miss
    #             is a miss: "Mclean Cochran" for a printed "Mclean-Cochran" is not a fact the
    #             benchmark should award.
    #   ADDRESS   normalisation is the SEPARATOR set and nothing else (',' ';' '.'). Any other
    #             character difference is an error, "us" against "USA" included -- Naveen: "other
    #             than ',', '.', ';' any other character difference also considered to be error
    #             only". The merge in normalise_address_compare exists so the model is not scored
    #             on which component it filed each token in; it was never a licence to forgive
    #             the tokens themselves.
    #
    # Consequence, stated because it is easy to miss: after 1.7 no headline-eligible key consults
    # ANLS_THRESHOLD for its VERDICT, so `match_policy: threshold` is inert for every published
    # number. Since contract 1.14 the threshold is not unused -- it decides the LENIENT column,
    # which is reported beside the exact one and never replaces it, so --anls-threshold moves
    # that column alone. `match_policy` remains inert for TEXT and ADDRESS by design.
    if rule is MatchRule.ADDRESS:
        # Merge the model's five components into one string (Q2, Naveen 2026-09-01) and compare
        # against the whole GT block. The merge joins on whitespace (Naveen 2026-09-09).
        #
        # Both sides then collapse SEPARATORS ONLY -- ',' ';' '.' -- and keep every other
        # character. The printed block separates city from state with a comma
        # ("Tammyland, SD 42587 US") that a component split cannot reproduce, so that comma is
        # a rendering convention of the page rather than a fact about the address; keeping it
        # scores every merged address wrong (measured: 0.00% exact). Everything else stays:
        # '-', '/', '#', '&' and brackets are part of an address, and collapsing them let
        # "1851-a" and "1851 a" score as the same place. See normalise_address_compare.
        # Map contract 1.12: the prediction is rendered every admissible way -- three component
        # orders x {as emitted, de-duplicated} -- and each is compared for EXACT equality.
        # address_renderings() carries the reasoning and the measurement.
        t = normalise_address_compare(merge_address_components(truth))
        forms = address_renderings(prediction)

        exact = (t or "") in forms if t else not forms
        score = max((anls(f, t) for f in forms), default=anls(None, t))
        # Report the rendering the verdict was actually taken from: the matching one if there
        # is one, otherwise the closest, so a failure is shown at its best light.
        p = (t if exact else
             max(forms, key=lambda f: anls(f, t), default=None))
        # Kept for the convention test below, which is defined on a single merged pair.
        p_us = normalise_address_compare(merge_address_components(prediction, alt_order=False))
        p_alt = normalise_address_compare(merge_address_components(prediction, alt_order=True))
        # Contract 1.11, case B: the merged blocks differ by the country token alone. Tested on
        # BOTH component orderings, and only ever on one token -- a second difference anywhere
        # in the block fails the test, so this cannot forgive a misread street or a dropped
        # state code.
        convention = exact or _convention_address(p_us, t) or _convention_address(p_alt, t)
        # CONTAINMENT, contract 1.16 as amended by 1.18: the ground-truth block appears inside
        # one admissible rendering of the prediction VERBATIM AND UNBROKEN. Address is now the
        # only rule that computes this, because it is the only field where the pipeline
        # legitimately emits more than the annotator stored -- a receipt prints the full
        # postal block and SROIE's ground truth keeps a prefix of it.
        #
        # Token runs, not raw substrings, for the reason CONTAINMENT_CRITERION gives: a raw
        # `truth in prediction` test matches across word boundaries and would score "MART"
        # inside "SUPERMARKET" as the model emitting the value.
        #
        # Every rendering is tried, not just the closest, so containment inherits contract
        # 1.12's promise that the model is not scored on which component it filed a token in.
        t_tokens = (t or "").split()
        contained = exact or any(_contains_token_run(f.split(), t_tokens) for f in forms)
        # LENIENT, contract 1.17: containment, plus exact equality once both sides shed a
        # printed registration number. No ANLS -- see strip_store_number.
        t_bare = normalise_address_compare(
            merge_address_components(_strip_address_store_numbers(truth)))
        bare_forms = address_renderings(_strip_address_store_numbers(prediction))
        lenient = contained or ((t_bare or "") in bare_forms if t_bare else not bare_forms)
    else:
        p, t = normalise_string(prediction), normalise_string(truth)
        exact = p == t
        score = anls(p, t)
        # Contract 1.19: a leaf the doc type declares in `contained_leaves` also reports a
        # CONTAINMENT reading -- the ground truth present in the prediction as a contiguous
        # token run, with extra text around it. Everything else about the rule is TEXT's,
        # `exact` included, which is the whole reason this is a separate rule rather than a
        # return to TEXT_CONTAINED: that rule computes `exact` with punctuation DROPPED, so
        # routing a leaf through it silently moves the leaf's exact number too. See the
        # TEXT_SUPERSET block above compare().
        #
        # The containment comparison DOES drop punctuation, while `exact` above does not, and
        # that asymmetry is deliberate. Containment asks "is the ground truth in there,
        # unbroken"; a receipt that prints "Farmasi Maluri S/B(587969-U)" with no space before
        # the bracket tokenises as ONE token and fails a punctuation-sensitive containment test
        # for a reason that has nothing to do with the question. Measured: 32 of 957 seller.name
        # instances turn on this alone, all of them spacing or punctuation
        # ("MR. D.I.Y.(KUCHAI)" against an annotated "MR. D. I. Y. (KUCHAI)").
        #
        # This is a criterion being looser than exact, which is what a criterion is for. It is
        # NOT the bug 1.19 was written against: that one had a criterion changing `exact`
        # itself. Dropping punctuation here cannot, because `exact` is computed above from
        # `p` and `t` and is never revisited. Token boundaries still hold, so the
        # MART-inside-SUPERMARKET coincidence still fails.
        contained = exact or (rule is MatchRule.TEXT_SUPERSET and _contains_token_run(
            normalise_string(prediction, drop_punct=True).split(),
            normalise_string(truth, drop_punct=True).split()))
        # Contract 1.17 for TEXT: the same store-number strip, on a flat string.
        lenient = contained or (normalise_string(strip_store_number(prediction))
                                == normalise_string(strip_store_number(truth)))
    if rule in (MatchRule.TEXT, MatchRule.ADDRESS, MatchRule.TEXT_SUPERSET):
        # Returned BEFORE _apply_policy, so --match-policy threshold cannot reach either rule.
        # The ANLS is still computed and carried on `score` so the report can show HOW CLOSE a
        # miss was; since contract 1.17 it decides NOTHING, on any rule.
        return MatchResult(matched=exact, exact=exact, score=score,
                           token_f1=token_f1(p, t),
                           reason="" if exact else f"not exact (anls {score:.3f})",
                           convention=(convention if rule is MatchRule.ADDRESS else exact),
                           contained=contained,
                           lenient=lenient)
    res = MatchResult(
        matched=score >= ANLS_THRESHOLD,
        exact=exact,
        score=score,
        token_f1=token_f1(p, t),
        reason="" if score >= ANLS_THRESHOLD else f"anls {score:.3f}",
        lenient=score >= ANLS_THRESHOLD,
    )
    return _apply_policy(res)


# ---------------------------------------------------------------------------------------
# COMPARISON FORMS -- what the comparator actually equates, made inspectable.
# ---------------------------------------------------------------------------------------
# `compare()` above decides a verdict from a normalised form of each side, but that form is a
# local variable and never leaves the function, so a reviewer reading the review workbook sees
# the STORED values and has to take the verdict on trust. This returns the same forms for
# display, one row per rule, so "why is this a match?" is answerable from the sheet.
#
# It is a SECOND implementation of the same normalisation, which is exactly the risk it has to
# manage. The guarantee is checked, not asserted: scripts/verify_comparison_forms.py replays
# every scored field in a run and requires
#         (pred_form == truth_form)  ==  compare(...).exact
# for every row, with the two documented exceptions below. Any drift between this function and
# compare() fails that check.
#
# Two rules cannot satisfy that identity by construction, and the checker exempts them:
#   TEXT_CONTAINED  the verdict is token RECALL of the GT inside the prediction, not equality
#   TEXT / ADDRESS  under match_policy=threshold the verdict is ANLS >= bar, not equality
#                   (under match_policy=exact, which is what is published, they do hold)

# Two markers, because a blank numeric cell has two different causes and they are different
# findings (see numeric_from_field): the key was present and held null -- the stage answered
# "nothing" -- or there was text there and no number could be read out of it. Arm-neutral
# wording on purpose: on FINAL the first is the product shipping a null, on RAW it is the model
# emitting none, and the cell should not assert which.
_UNPARSEABLE = "<no number readable>"
_SHIPPED_NULL = "<null>"


def comparison_forms(prediction: Any, truth: Any, rule: MatchRule) -> Tuple[str, str]:
    """Render the two values `compare()` equates under `rule`, for display.

    Returns (prediction_form, truth_form) as strings. An empty side renders as "" and a numeric
    side that could not be read renders as a bracketed marker, so a blank cell always means
    "nothing was there" and never "the normaliser gave up".
    """
    def _empty(v) -> bool:
        return v is None or isinstance(v, _Absent) or (isinstance(v, str) and not v.strip())

    if rule is MatchRule.NUMERIC:
        out = []
        for side in (prediction, truth):
            if _empty(side):
                out.append("")
                continue
            val, src = numeric_from_field(side)
            if val is None:
                out.append(_SHIPPED_NULL if src is not None else _UNPARSEABLE)
            else:
                out.append(str(val))
        return out[0], out[1]

    if rule is MatchRule.DATE_ISO:
        return tuple("" if _empty(v) else (normalise_date(v) or _UNPARSEABLE)
                     for v in (prediction, truth))

    if rule is MatchRule.PHONE:
        return tuple("" if _empty(v) else (normalise_phone_compare(v) or "")
                     for v in (prediction, truth))

    if rule is MatchRule.ADDRESS:
        t = normalise_address_compare(merge_address_components(truth)) or ""
        if _empty(prediction) and not isinstance(prediction, dict):
            return "", t
        forms = address_renderings(prediction)
        # Show the rendering the verdict was taken from: the matching one if any matches,
        # otherwise the closest (map contract 1.12, same set as _compare).
        if t in forms:
            return t, t
        return max(forms, key=lambda f: anls(f, t), default=""), t

    if rule is MatchRule.TEXT_CONTAINED:
        return tuple("" if _empty(v) else (normalise_string(v, drop_punct=True) or "")
                     for v in (prediction, truth))

    # DATE_RAW, IDENTIFIER, ENUM, BOOL, CURRENCY, EMAIL, TEXT -- all plain string normalisation.
    return tuple("" if _empty(v) else (normalise_string(v) or "")
                 for v in (prediction, truth))


# ---------------------------------------------------------------------------------------
# KEYED CHARGE LINES -- multi-rate tax, and anything else Totals cannot hold as a scalar.
# ---------------------------------------------------------------------------------------
# InvoiceData.Totals holds ONE taxName / taxPercentage / taxAmount. Two FATURA templates print
# more than one tax line (Template25: five GST rates; Template29: a VAT line AND a GST line),
# so those three scalar paths are excluded there -- filling them means choosing one of five
# rates by fiat. The printed lines are still facts, and they are compared here instead, against
# totals.otherCharges, whose own schema description names "Tax" as a document-level modifier.
#
# Two rules, both settled by measurement on s42_main2000 (80 sampled documents, 280 lines):
#
#   MATCH ON THE RATE EXTRACTED FROM THE KEY, never the key string. Ground truth writes
#   "GST(18%)" / "VAT(5.99%)"; the pipeline emits "GST(18%)", "GST(18%) :" and
#   "TAX:VAT (5.99%)". Comparing key strings scores ~0%; comparing extracted rates scores
#   86.79% on otherCharges alone. Rates are unique within a document on every one of the 400
#   documents, so the alignment is never ambiguous.
#
#   THE PREDICTION SIDE IS A UNION of otherCharges and the scalar triple. On Template29 the
#   pipeline splits the two taxes, sending GST to the list and VAT to the scalars (24 of 40
#   sampled documents). Against otherCharges alone Template29 recovers 66.25%; against the
#   union, 96.25%. Same merged-target reasoning as addressStructured.
#
# Values go through numeric_from_field, so a charge line is compared exactly as every other
# amount in the benchmark is: normalizedValue preferred, originalValue parsed as a fallback,
# quantised to 2dp, no tolerance.

_RATE_IN_KEY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

SCALAR_TAX_PATHS = ("totals.taxName", "totals.taxPercentage", "totals.taxAmount")


def rate_from_key(key: Any) -> Optional[Decimal]:
    """The tax rate a charge key names. 'TAX:VAT (5.99%)' -> 5.99, 'GST(18%) :' -> 18.00."""
    if key is None:
        return None
    m = _RATE_IN_KEY_RE.search(str(key))
    if not m:
        return None
    try:
        return Decimal(m.group(1)).quantize(Decimal("0.01"))
    except Exception:
        return None


def charge_lines(container: Any, *, include_scalar_tax: bool = True) -> List[Dict[str, Any]]:
    """Every keyed charge line a prediction (or a GT record's gt dict) offers.

    Returns [{"rate": Decimal|None, "value": Decimal|None, "key": <as written>,
              "source": "otherCharges"|"scalarTax"}].
    The scalar tax triple contributes at most one line, and only when it carries a rate; a
    scalar tax with no percentage cannot be aligned to a printed line and is left out rather
    than guessed onto one.
    """
    if not isinstance(container, dict):
        return []
    out: List[Dict[str, Any]] = []
    totals = container.get("totals") if isinstance(container.get("totals"), dict) else {}
    charges = totals.get("otherCharges") if isinstance(totals, dict) else None
    if charges is None:
        charges = container.get("totals.otherCharges")
    for entry in (charges or []):
        if not isinstance(entry, dict):
            continue
        value, _src = numeric_from_field(entry.get("value"))
        out.append({"rate": rate_from_key(entry.get("key")), "value": value,
                    "key": entry.get("key"), "source": "otherCharges"})
    if include_scalar_tax:
        pct = totals.get("taxPercentage") if totals else container.get("totals.taxPercentage")
        amt = totals.get("taxAmount") if totals else container.get("totals.taxAmount")
        name = totals.get("taxName") if totals else container.get("totals.taxName")
        rate, _ = numeric_from_field(pct)
        value, _ = numeric_from_field(amt)
        if rate is not None:
            out.append({"rate": rate, "value": value,
                        "key": f"{name or 'TAX'}({rate}%)", "source": "scalarTax"})
    return out


def compare_charge_lines(prediction: Any, truth_lines: List[Dict[str, Any]], *,
                         include_scalar_tax: bool = True) -> Dict[str, Any]:
    """Align printed charge lines against a prediction and report per line.

    `truth_lines` is the ground-truth list as stored: [{"key","value","name","percentage"}].
    Returns per-line verdicts plus the counts a precision/recall pair needs. A predicted entry
    is consumed by at most one printed line, so a model emitting the same rate twice cannot
    satisfy two lines with one entry.
    """
    pred = charge_lines(prediction, include_scalar_tax=include_scalar_tax)
    used: set = set()
    lines = []
    for t in truth_lines:
        t_rate, _ = numeric_from_field(t.get("percentage"))
        t_value, _ = numeric_from_field(t.get("value"))
        hit = None
        for i, p in enumerate(pred):                      # exact: rate AND value
            if i in used or p["rate"] != t_rate or p["value"] is None or p["value"] != t_value:
                continue
            hit = i
            break
        by_rate = next((i for i, p in enumerate(pred)
                        if i not in used and p["rate"] == t_rate), None)
        by_value = next((i for i, p in enumerate(pred)
                         if i not in used and p["value"] is not None and p["value"] == t_value),
                        None)
        if hit is not None:
            used.add(hit)
            state, matched = "correct", True
        elif by_rate is not None:
            used.add(by_rate)
            state, matched = ("missing" if pred[by_rate]["value"] is None else "wrong_value"), False
        elif by_value is not None:
            used.add(by_value)
            state, matched = "wrong_rate", False
        else:
            state, matched = "not_emitted", False
        chosen = hit if hit is not None else (by_rate if by_rate is not None else by_value)
        lines.append({
            "rate": str(t_rate) if t_rate is not None else None,
            "truth_value": str(t_value) if t_value is not None else None,
            "pred_value": (str(pred[chosen]["value"]) if chosen is not None
                           and pred[chosen]["value"] is not None else None),
            "pred_key": pred[chosen]["key"] if chosen is not None else None,
            "source": pred[chosen]["source"] if chosen is not None else None,
            "state": state, "matched": matched,
        })
    spurious = [p for i, p in enumerate(pred) if i not in used]
    return {
        "lines": lines,
        "n_truth": len(truth_lines),
        "n_correct": sum(1 for l in lines if l["matched"]),
        "n_predicted": len(pred),
        "n_spurious": len(spurious),
        "spurious": [{"key": p["key"], "value": str(p["value"]) if p["value"] is not None else None,
                      "source": p["source"]} for p in spurious],
        "all_lines_recovered": all(l["matched"] for l in lines) if lines else False,
        "exact_set": (all(l["matched"] for l in lines) and not spurious) if lines else False,
    }


def load_type_registry(tsv_path: str) -> Dict[str, str]:
    """Read the generated path -> declared-type inventory."""
    registry: Dict[str, str] = {}
    with open(tsv_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            path, _, kind = line.partition("\t")
            registry[path] = kind
    return registry


# The five addressStructured components are merged into one comparison (Q2), so the SCOREABLE
# unit is the object, not its leaves. They are consumed, not excluded -- the content still has
# to be right, it just does not matter which component the model put each piece in.
ADDRESS_COMPONENTS = ("address", "city", "state", "postal_code", "country")

# Derived comparison targets come from the DocTypeSpec, never from this module -- see
# doctypes/__init__.py. Every entry point here takes an optional `spec` and falls back to the
# invoice spec, which is the only type with a frozen map today. Passing the spec explicitly is
# always preferred; the default exists so a caller working on invoices need not thread it
# through, not as a licence for core/ to assume a document type.
DEFAULT_SPEC_NAME = "invoice"


def _spec(spec=None):
    if spec is not None:
        return spec
    import doctypes
    return doctypes.get(DEFAULT_SPEC_NAME)


def merge_prediction_sources(path: str, flat_prediction: Dict[str, Any],
                             spec=None) -> Optional[str]:
    """Join the prediction values behind a derived target into one comparable string."""
    sources = _spec(spec).merged_targets.get(path, ())
    parts = []
    for src in sources:
        v = flat_prediction.get(src)
        if v is not None and str(v).strip():
            parts.append(str(v).strip())
    return " ".join(parts) or None


def load_rule_registry(tsv_path: str, spec=None) -> Dict[str, MatchRule]:
    """Scoreable path -> match rule for one document type.

    Two collapses happen here, both so the scorer measures FACTS rather than field placement:
      * addressStructured -> one object comparison (spec.collapse_address_objects)
      * spec.merged_targets -> one derived target replacing its component paths
    """
    spec = _spec(spec)
    types = load_type_registry(tsv_path)
    registry: Dict[str, MatchRule] = {}
    consumed: set = set()
    for target, sources in spec.merged_targets.items():
        registry[target] = MatchRule.TEXT_CONTAINED
        consumed.update(sources)

    if spec.collapse_address_objects:
        for path in types:
            if ".addressStructured." in path:
                obj, _, leaf = path.rpartition(".")
                if leaf in ADDRESS_COMPONENTS:
                    registry[obj] = MatchRule.ADDRESS
                    consumed.add(path)
    for path, kind in types.items():
        if path in consumed or path.rsplit(".", 1)[-1] in spec.excluded_leaves:
            continue
        registry[path] = rule_for(path, kind, spec)
    return registry


def consumed_paths(tsv_path: str, spec=None) -> set:
    """Leaf paths folded into a parent or derived comparison; scored, but not as their own row."""
    spec = _spec(spec)
    out = set()
    if spec.collapse_address_objects:
        out |= {p for p in load_type_registry(tsv_path)
                if ".addressStructured." in p and p.rpartition(".")[2] in ADDRESS_COMPONENTS}
    for sources in spec.merged_targets.values():
        out.update(sources)
    return out
