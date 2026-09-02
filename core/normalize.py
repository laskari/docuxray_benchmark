"""Deterministic value normalisation.

Every rule here is published in the benchmark methodology. Most benchmark disputes are
normalisation disputes, so the rules live in one dependency-free, unit-tested module and both
sides of a comparison go through the same function.

Nothing in this module knows about FATURA. Dataset-specific parsing lives in mapping/parsers.py.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

# --------------------------------------------------------------------------------------
# Sentinels
# --------------------------------------------------------------------------------------

class _Absent:
    """Ground truth says this field is absent from the document (a scoreable negative).

    Distinct from None, which means 'no value parsed', and from a path being missing from
    annotated_fields, which means 'the dataset cannot tell us either way'.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT = _Absent()


class ParseError(ValueError):
    """Raised by a strict parser when input does not match its expected shape."""


# --------------------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------------------

_TWO_DP = Decimal("0.01")

# A number with optional thousands separators and optional decimals, optionally wrapped in
# parentheses (accounting negative) and optionally sign-prefixed.
_NUMBER_RE = re.compile(
    r"""
    (?P<open>\()?             # accounting-negative open paren
    \s*
    (?P<sign>[-+])?           # explicit sign
    \s*
    (?P<int>\d{1,3}(?:,\d{3})+|\d+)   # 1,234,567  or  1234567
    (?P<frac>\.\d+)?          # .56
    \s*
    (?P<close>\))?            # accounting-negative close paren
    """,
    re.VERBOSE,
)

# Uppercase label leakage: 'BALANCE_DUE : 481.84 $', 'DUE_AMOUNT : 240.38 $'.
# Deliberately narrow -- uppercase and underscores only, so it never eats
# 'VAT (3.88%): 28.18' (paren before the colon) or 'Due Date : ...' (mixed case, has a space).
_LABEL_PREFIX_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*:\s*")


def strip_label_prefix(text: str) -> str:
    """Remove a leaked ALL_CAPS label prefix. Returns text unchanged when there is none."""
    if not isinstance(text, str):
        return text
    return _LABEL_PREFIX_RE.sub("", text, count=1)


def _iter_numbers(text: str):
    for m in _NUMBER_RE.finditer(text):
        raw_int = m.group("int").replace(",", "")
        frac = m.group("frac") or ""
        try:
            val = Decimal(raw_int + frac)
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
        negative = m.group("sign") == "-" or (m.group("open") and m.group("close"))
        yield (-val if negative else val), m


def normalise_money(text, *, which: str = "last") -> Optional[Decimal]:
    """Parse a money-ish string to a Decimal quantised to 2dp, half-up.

    Handles thousands separators, accounting negatives '(102.68)' -> -102.68, an explicit
    '(-) 13.42' minus marker, currency suffixes and leaked ALL_CAPS label prefixes.

    which='last' takes the final number in the string (currency is a suffix, so this is right
    for '734.33 EUR'); which='first' takes the leading number.
    Returns None when no number is present.
    """
    if text is None or isinstance(text, _Absent):
        return None
    if isinstance(text, (int, float, Decimal)):
        return _quantise(Decimal(str(text)))

    s = strip_label_prefix(str(text)).strip()
    if not s:
        return None

    found = list(_iter_numbers(s))
    if not found:
        return None
    value, match = found[-1] if which == "last" else found[0]

    # '(-) 13.42' -- a bare minus marker in parentheses ahead of the number.
    if value > 0 and re.search(r"\(\s*-\s*\)\s*[^\d]{0,3}$", s[: match.start()]):
        value = -value

    return _quantise(value)


def _quantise(value: Decimal) -> Decimal:
    return value.quantize(_TWO_DP, rounding=ROUND_HALF_UP)


def money_to_str(value: Optional[Decimal]) -> Optional[str]:
    """Canonical wire form for a numeric: a plain 2dp string, or None."""
    return None if value is None else f"{value:.2f}"


# --------------------------------------------------------------------------------------
# NumericValue field access
# --------------------------------------------------------------------------------------

# The field the scorer READS for a numeric. `originalValue` is the default and the
# recommended setting: it is the verbatim printed string, it is present at EVERY pipeline
# stage, and the postprocessor only ever `.strip()`s it -- so arms A, B and C are all scored
# on the identical field and any arm difference is a real pipeline difference rather than a
# wire-shape artifact. `normalizedValue` exists only after postprocessing.
#
# Switch to "normalizedValue" only for a dataset whose printed numbers are ambiguous enough
# that production's parser (which resolves '1.234' style separator ambiguity with a documented
# rule) is doing work this module's parser cannot. FATURA is not such a dataset: its amounts
# are uniformly '1,098.21 USD' -- comma thousands, dot decimal, no ambiguity.
NUMERIC_SOURCE = "originalValue"


def numeric_from_field(value, *, prefer: str = None) -> tuple:
    """Read the numeric FACT out of whatever shape a numeric field arrives in.

    The wire shape differs by pipeline stage:

      arm A (raw extraction)    {"originalValue": "1,234.56"}
                                new_schema.NumericValue declares ONLY originalValue and sets
                                extra="forbid" -- the model cannot emit anything else.
      arm B / C (postprocessed) {"originalValue": "1,234.56", "normalizedValue": 1234.56}
                                added by ai/postprocessing/_common.py.

    Reads `prefer` (default NUMERIC_SOURCE), falling back to the other key when the preferred
    one is missing or null. Comparison is always NUMERIC, never string: the same amount is
    printed '1,234.56' by one template and '1234.56 EUR' by another, and a string match would
    score that as wrong.

    Returns (Decimal | None, source) where source names the key actually used.
    """
    prefer = prefer or NUMERIC_SOURCE
    if value is None or isinstance(value, _Absent):
        return None, None

    if isinstance(value, dict):
        order = (("originalValue", "normalizedValue") if prefer == "originalValue"
                 else ("normalizedValue", "originalValue"))
        for key in order:
            if key not in value:
                continue
            raw = value[key]
            if raw is None:
                continue
            if key == "normalizedValue":
                # True is an int in Python; it must never read as the number 1.
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                return _quantise(Decimal(str(raw))), key
            parsed = normalise_money(raw)
            if parsed is not None:
                return parsed, key
        return None, None

    parsed = normalise_money(value)
    return parsed, ("originalValue" if parsed is not None else None)


def numeric_parse_disagreement(value) -> bool:
    """True when a field carries BOTH keys and they do not agree at 2dp.

    Worth counting rather than resolving: it is where this module's parser and production's
    parser read the same printed string differently, which is a finding about one of them.
    """
    if not isinstance(value, dict):
        return False
    nv = value.get("normalizedValue")
    if isinstance(nv, bool) or not isinstance(nv, (int, float)):
        return False
    ov = normalise_money(value.get("originalValue"))
    return ov is not None and ov != _quantise(Decimal(str(nv)))


# --------------------------------------------------------------------------------------
# Percentages
# --------------------------------------------------------------------------------------

_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def normalise_percent(text) -> Optional[Decimal]:
    """First percentage in the string. '(3.88%): 28.18 EUR' -> 3.88"""
    if text is None or isinstance(text, _Absent):
        return None
    m = _PCT_RE.search(str(text))
    return _quantise(Decimal(m.group(1))) if m else None


# --------------------------------------------------------------------------------------
# Currency
# --------------------------------------------------------------------------------------

# Unambiguous symbols only. '$' is deliberately excluded: it could be USD, CAD, AUD, SGD...
# and FATURA states no locale, so a document whose only evidence is '$' has no scoreable
# currency. Guessing USD would be inventing a fact.
_SYMBOL_TO_ISO = {"€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_ISO_CODES = {"EUR", "USD", "GBP", "INR", "JPY", "AUD", "CAD", "CHF", "SGD", "AED"}
AMBIGUOUS_CURRENCY = "__AMBIGUOUS__"


def normalise_currency(text) -> Optional[str]:
    """Extract an ISO-4217 code from a money string.

    Returns the code, AMBIGUOUS_CURRENCY when the only evidence is a bare '$', or None when
    there is no currency evidence at all. Callers treat AMBIGUOUS_CURRENCY as not-annotated.
    """
    if text is None or isinstance(text, _Absent):
        return None
    s = str(text).strip().upper()

    for code in _ISO_CODES:
        if re.search(rf"\b{code}\b", s):
            return code
    for sym, code in _SYMBOL_TO_ISO.items():
        if sym in s:
            return code
    if "$" in s:
        return AMBIGUOUS_CURRENCY
    return None


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------

# Ordered by specificity. FATURA is uniformly DD-Mon-YYYY, so there is no DD/MM vs MM/DD
# ambiguity to resolve -- but the model may emit any of these, so predictions need the wider set.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
    "%b %d, %Y", "%B %d, %Y",
    "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%Y/%m/%d",
)


def normalise_date(text, *, formats=_DATE_FORMATS) -> Optional[str]:
    """Parse a date to ISO-8601 'YYYY-MM-DD'. Returns None when nothing parses.

    Never guesses between DD/MM and MM/DD: only DD/MM is attempted, because the ambiguity is
    resolved per-dataset in the adapter and recorded there, not guessed per-document.
    """
    if text is None or isinstance(text, _Absent):
        return None
    s = str(text).strip()
    if not s:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------------------
# Strings
# --------------------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalise_string(text, *, drop_punct: bool = False) -> Optional[str]:
    """Unicode-NFKC, collapse whitespace (newlines included), casefold, strip."""
    if text is None or isinstance(text, _Absent):
        return None
    s = unicodedata.normalize("NFKC", str(text))
    if drop_punct:
        s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip().casefold() or None


def normalise_phone(text) -> Optional[str]:
    """Keep digits, preserving a leading '+'. '+(352)259-8443' -> '+3522598443'."""
    if text is None or isinstance(text, _Absent):
        return None
    s = str(text).strip()
    plus = s.lstrip().startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    return ("+" if plus else "") + digits


def normalise_address(text) -> Optional[str]:
    """Flatten a multi-line address block to one comma-separated line, then normalise."""
    if text is None or isinstance(text, _Absent):
        return None
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip(" ,") for p in s.split("\n") if p.strip(" ,")]
    return normalise_string(", ".join(parts))


ADDRESS_COMPONENT_ORDER = ("address", "city", "state", "postal_code", "country")


def merge_address_components(value) -> Optional[str]:
    """Collapse an addressStructured object into one comparable address string.

    Ground truth gives ONE multi-line block; the schema splits it across five components. Rather
    than parse the GT block into components -- which would make our parser the arbiter of
    correctness -- we merge the model's components back into a single string and compare that
    against the whole block. A model that correctly routes the city into `city` is then neither
    rewarded nor punished for the split; only the address CONTENT is scored.

    Accepts the object, a bare string, or None.
    """
    if value is None or isinstance(value, _Absent):
        return None
    if isinstance(value, str):
        return normalise_address(value)
    if not isinstance(value, dict):
        return None
    parts = []
    for key in ADDRESS_COMPONENT_ORDER:
        piece = value.get(key)
        if piece is None:
            continue
        piece = str(piece).strip(" ,")
        if piece:
            parts.append(piece)
    return normalise_address("\n".join(parts)) if parts else None


# --------------------------------------------------------------------------------------
# Similarity -- ANLS
# --------------------------------------------------------------------------------------

def levenshtein(a: str, b: str) -> int:
    """Iterative Levenshtein distance, O(min(len)) space."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + (ca != cb),  # substitution
            ))
        previous = current
    return previous[-1]


def anls(prediction, truth) -> float:
    """Average Normalised Levenshtein Similarity for a single pair, in [0, 1].

    The standard string metric in document AI (ICDAR/DocVQA), so string numbers here are
    comparable to published work. Both sides go through normalise_string first.
    """
    p, t = normalise_string(prediction), normalise_string(truth)
    if p is None and t is None:
        return 1.0
    if p is None or t is None:
        return 0.0
    longest = max(len(p), len(t))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(p, t) / longest


def token_f1(prediction, truth) -> float:
    """Bag-of-tokens F1 after normalisation. Reported as a third tier for string fields."""
    p, t = normalise_string(prediction, drop_punct=True), normalise_string(truth, drop_punct=True)
    if not p and not t:
        return 1.0
    if not p or not t:
        return 0.0
    from collections import Counter
    pc, tc = Counter(p.split()), Counter(t.split())
    overlap = sum((pc & tc).values())
    if overlap == 0:
        return 0.0
    precision, recall = overlap / sum(pc.values()), overlap / sum(tc.values())
    return 2 * precision * recall / (precision + recall)
