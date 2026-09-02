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
from typing import Any, Dict, Optional, Tuple

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import (  # noqa: E402
    ABSENT, _Absent, anls, normalise_address, normalise_date, normalise_money,
    merge_address_components, normalise_phone, normalise_string, numeric_from_field,
    numeric_parse_disagreement, token_f1,
)

ANLS_THRESHOLD = 0.8          # the document-AI standard, so string numbers stay comparable
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


_ADDRESS_HINT = re.compile(r"addressStructured(\.address)?$")
_ISO_HINT = re.compile(r"ISO$")


def rule_for(path: str, declared_type: str, spec=None) -> MatchRule:
    """Derive the match rule from the generated schema type plus the doc type's own leaf sets.

    Type-driven, not name-driven: adding a field to new_schema.py gets a rule automatically,
    so a schema change never reopens the benchmark contract.
    """
    spec = _spec(spec)
    leaf = path.rsplit(".", 1)[-1]
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
    """Compare one predicted value against one GT value under the given rule."""
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
            return MatchResult(False, False, 0.0, reason="unparseable number",
                               value_source=p_src, numeric_parse_disagreement=disagree)
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

    if rule in (MatchRule.IDENTIFIER, MatchRule.ENUM, MatchRule.BOOL, MatchRule.CURRENCY):
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok))

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
        return MatchResult(
            matched=recall >= 0.9,
            exact=(t_norm == p_norm),
            score=recall,
            token_f1=token_f1(p_norm, t_norm),
            reason="" if recall >= 0.9 else f"token recall {recall:.2f}",
        )

    if rule is MatchRule.PHONE:
        ok = normalise_phone(prediction) == normalise_phone(truth)
        return MatchResult(ok, ok, float(ok))

    if rule is MatchRule.EMAIL:
        ok = normalise_string(prediction) == normalise_string(truth)
        return MatchResult(ok, ok, float(ok))

    # TEXT and ADDRESS -- three tiers, all published: exact, ANLS >= 0.8 (the headline for
    # string fields, comparable to published work), and normalised token F1.
    if rule is MatchRule.ADDRESS:
        # Merge the model's five components into one string (Q2, Naveen 2026-09-01) and compare
        # against the whole GT block. Punctuation is dropped on both sides: merging inserts
        # commas the printed block does not have, and that is a formatting artifact of the
        # merge, not a difference in the address.
        p = merge_address_components(prediction)
        t = merge_address_components(truth)
        p = normalise_string(p, drop_punct=True)
        t = normalise_string(t, drop_punct=True)
    else:
        p, t = normalise_string(prediction), normalise_string(truth)
    exact = p == t
    score = anls(p, t)
    return MatchResult(
        matched=score >= ANLS_THRESHOLD,
        exact=exact,
        score=score,
        token_f1=token_f1(p, t),
        reason="" if score >= ANLS_THRESHOLD else f"anls {score:.3f}",
    )


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
