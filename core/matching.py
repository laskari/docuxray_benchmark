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
    merge_address_components, normalise_address_compare, normalise_phone,
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
        return MatchRule.TEXT_CONTAINED
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

    Returns BOTH verdicts of contract 1.11 on one result: `matched` is the exact criterion,
    unchanged and computed exactly as before; `convention` is the convention-adjusted reading.
    Only the two rules that carry a notation difference (CURRENCY, ADDRESS) can ever separate
    them -- every other rule sets them equal, enforced below rather than trusted to each branch.
    """
    res = _compare(prediction, truth, rule)
    if res.matched:
        # The invariant that makes the two numbers comparable: convention-adjusted is never
        # weaker than exact, so the gap between them is always the enumerated list and nothing
        # else. Enforced in one place so a new rule cannot silently report a LOWER second
        # number by forgetting to set the field.
        res.convention = True
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
        )
        return _apply_policy(res)

    if rule is MatchRule.PHONE:
        # Whitespace removed, nothing else (Naveen 2026-09-09). The ground truth stores the
        # printed number verbatim (map rule 1.2), so brackets, hyphens and slashes are part of
        # the value and are compared. See normalise_phone_compare for the measured cost.
        ok = normalise_phone_compare(prediction) == normalise_phone_compare(truth)
        return MatchResult(ok, ok, float(ok))

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
    # ANLS_THRESHOLD, so `match_policy: threshold` and `--anls-threshold` are inert for every
    # published number and exact == lenient everywhere. The knobs remain only for TEXT_CONTAINED
    # (noteText, barred from headlines) and for the ad-hoc probes in scripts/.
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
        p_us = normalise_address_compare(merge_address_components(prediction, alt_order=False))
        p_alt = normalise_address_compare(merge_address_components(prediction, alt_order=True))
        t = normalise_address_compare(merge_address_components(truth))
        
        exact = p_us == t or p_alt == t
        score = max(anls(p_us, t), anls(p_alt, t))
        p = p_us if anls(p_us, t) >= anls(p_alt, t) else p_alt
        # Contract 1.11, case B: the merged blocks differ by the country token alone. Tested on
        # BOTH component orderings, and only ever on one token -- a second difference anywhere
        # in the block fails the test, so this cannot forgive a misread street or a dropped
        # state code.
        convention = exact or _convention_address(p_us, t) or _convention_address(p_alt, t)
    else:
        p, t = normalise_string(prediction), normalise_string(truth)
        exact = p == t
        score = anls(p, t)
    if rule in (MatchRule.TEXT, MatchRule.ADDRESS):
        # Returned BEFORE _apply_policy, so --match-policy threshold cannot reach either rule.
        return MatchResult(matched=exact, exact=exact, score=score,
                           token_f1=token_f1(p, t),
                           reason="" if exact else f"not exact (anls {score:.3f})",
                           convention=(convention if rule is MatchRule.ADDRESS else exact))
    res = MatchResult(
        matched=score >= ANLS_THRESHOLD,
        exact=exact,
        score=score,
        token_f1=token_f1(p, t),
        reason="" if score >= ANLS_THRESHOLD else f"anls {score:.3f}",
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
        p_us = normalise_address_compare(
            merge_address_components(prediction, alt_order=False)) or ""
        p_alt = normalise_address_compare(
            merge_address_components(prediction, alt_order=True)) or ""
        # Show the order that scores best, which is the one the verdict was taken from.
        p = p_us if (p_us == t or anls(p_us, t) >= anls(p_alt, t)) else p_alt
        return p, t

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
