"""CORD-v2 value parsers.

CORD ships an OCR transcription, not a cleaned field. Measured over all 1,000 documents, the
money-shaped values fall into 10 well-formed patterns plus 366 values across 44 decorated
shapes. This module is the single place that turns any of them into a canonical amount, and it
is deliberate about the two decisions that would otherwise be made by accident:

WHICH SEPARATOR IS THE DECIMAL POINT. Indonesian receipts use '.' and ',' interchangeably for
thousands, and CORD contains both in the same corpus -- '25.000' and '120,000' are both
twenty-five to a hundred and twenty THOUSAND, not twenty-five point nought. Census over the
corpus:

    4385  120,000        comma thousands
    1500  25.000         dot thousands
     690  0              plain integer
     131  53,636.00      comma thousands + dot decimal
     101  8.500,00       dot thousands + comma decimal
      56  0.00           dot decimal
      17  0,00           comma decimal
       3  1071.000       dot, 3 trailing digits, >3 leading   <- resolved as thousands
       1  10.000%        a rate, not an amount                <- rejected

A naive float() reads 4,385 of those as three-digit numbers. The rule below is stated as a
rule rather than a heuristic so it can be argued with, and every branch has a test.

WHAT A TRAILING MINUS MEANS. `menu.discountprice` values look like '800-', '1,400 -', '1,260-'.
On an Indonesian receipt 'Rp 1.400,-' is the no-cents marker, not a negative sign -- but a
discount is a reduction either way. Rather than guess, the discount parsers return a POSITIVE
MAGNITUDE and the map says so. This also matches what the invoice side already does:
fatura_parsers.money_abs stores GT discounts positive because the product's postprocessor
normalises a negative discountTotal to its absolute value, and storing GT negative would make
the postprocessed arm score worse on every discounted document as a pure sign artifact.

An unparseable value returns None. The adapter records that as an exclusion with a reason, so
one odd document cannot abort a build and the excluded count is published.

Registered in this module's OWN registry, with get() falling back to fatura_parsers. Nothing
here mutates the invoice parser table.
"""
from __future__ import annotations

import pathlib
import re
import sys
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import money_to_str                     # noqa: E402
from datasets import fatura_parsers as _fatura              # noqa: E402

Parser = Callable[..., Optional[str]]
_REGISTRY: Dict[str, Parser] = {}


def parser(name: str) -> Callable[[Parser], Parser]:
    def register(fn: Parser) -> Parser:
        _REGISTRY[name] = fn
        fn.parser_name = name       # type: ignore[attr-defined]
        return fn
    return register


def get(name: str) -> Parser:
    """CORD's parsers first, then the shared invoice ones. Falling back rather than merging
    keeps `fatura_parsers._REGISTRY` exactly as it was."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    return _fatura.get(name)


def known() -> list:
    return sorted(set(_REGISTRY) | set(_fatura.known()))


# ------------------------------------------------------------------------------ the money rule
_CURRENCY = re.compile(r"^(rp|idr)\b\.?\s*", re.I)
_LEAD_JUNK = re.compile(r"^[\s@*:\[\(#=]+")
_TAIL_JUNK = re.compile(r"[\s\]\)xX,.\-]+$")
_NUMERIC = re.compile(r"^\d[\d.,]*$")
_SPACE_THOUSANDS = re.compile(r"(?<=\d)[  ](?=\d{3}\b)")


def _to_decimal(token: str) -> Optional[Decimal]:
    """Resolve ',' and '.' into one number, or None if the token cannot mean an amount."""
    if not _NUMERIC.match(token):
        return None
    has_dot, has_com = "." in token, "," in token

    if has_dot and has_com:
        # Both present: whichever comes LAST is the decimal point. '53,636.00' and '8.500,00'.
        dec = "." if token.rfind(".") > token.rfind(",") else ","
        thou = "," if dec == "." else "."
        token = token.replace(thou, "").replace(dec, ".")

    elif has_dot or has_com:
        sep = "." if has_dot else ","
        parts = token.split(sep)
        tail = parts[-1]
        if len(parts) > 2:
            # Repeated separator. '121.000.00' and '39,200,00' are a mistyped mixed form, so a
            # 1-2 digit LAST group is the decimal and the earlier ones are thousands.
            if len(tail) in (1, 2):
                token = "".join(parts[:-1]) + "." + tail
            elif all(len(p) == 3 for p in parts[1:]):
                token = "".join(parts)
            else:
                return None
        elif len(tail) == 3:
            # The corpus convention: a single separator with exactly three trailing digits is
            # thousands. '25.000' -> 25000, '120,000' -> 120000, '1071.000' -> 1071000.
            token = "".join(parts)
        elif len(tail) in (1, 2):
            token = parts[0] + "." + tail
        else:
            # '57,0000', '385,0000' -- four trailing digits is neither convention. Refusing is
            # the honest answer; the adapter turns it into a published exclusion.
            return None

    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _clean(raw) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, list):
        # CORD emits a list when the receipt prints the same category twice. Two subtotals is a
        # genuinely ambiguous fact, so the adapter excludes it rather than picking one.
        return None
    s = str(raw).strip()
    if not s or not any(ch.isdigit() for ch in s):
        return None                       # '-', '---', ':' and friends
    if "%" in s:
        return None                       # a rate, not an amount
    s = _CURRENCY.sub("", s).strip()
    s = _CURRENCY.sub("", s).strip()      # 'Rp. Rp 0' appears once
    s = _SPACE_THOUSANDS.sub("", s)       # '19 000' -> '19000'
    s = _LEAD_JUNK.sub("", s)
    s = _TAIL_JUNK.sub("", s)
    if not s:
        return None
    if not _NUMERIC.match(s):
        # A label is glued to the value: 'TOTAL 47,499', 'Pb1 10% 7,000'. Take the last
        # numeric-looking token rather than dropping a real amount.
        toks = [t for t in re.findall(r"\d[\d.,]*", s) if _NUMERIC.match(t)]
        if not toks:
            return None
        s = toks[-1].rstrip(".,")
    return s or None


@parser("cord_money")
def cord_money(raw, *, key: str = "", **_) -> Optional[str]:
    """Any CORD money-shaped value -> canonical 2dp string. None when it cannot mean an amount."""
    neg = isinstance(raw, str) and raw.strip().startswith("(") and raw.strip().endswith(")")
    tok = _clean(raw)
    if tok is None:
        return None
    val = _to_decimal(tok)
    if val is None:
        return None
    return money_to_str(-val if neg else val)


@parser("cord_money_abs")
def cord_money_abs(raw, *, key: str = "", **_) -> Optional[str]:
    """Positive magnitude. For discounts, where a trailing '-' is the Indonesian no-cents
    marker on some receipts and a minus sign on others, and the schema wants a magnitude."""
    tok = _clean(raw)
    if tok is None:
        return None
    val = _to_decimal(tok)
    return money_to_str(abs(val)) if val is not None else None


@parser("cord_qty")
def cord_qty(raw, *, key: str = "", **_) -> Optional[str]:
    """'2' -> '2.00'. '1x', 'x1', '1X' -> '1.00'. '2.00' -> '2.00'.

    410 of 2,331 `menu.cnt` values in the corpus carry an x, on either side.
    """
    if raw is None or isinstance(raw, list):
        return None
    s = re.sub(r"[xX]", " ", str(raw)).strip()
    tok = _clean(s)
    if tok is None:
        return None
    val = _to_decimal(tok)
    return money_to_str(val) if val is not None else None


@parser("cord_percent")
def cord_percent(raw, *, key: str = "", **_) -> Optional[str]:
    """'10%' -> '10.00'. Returns None when there is no percent sign, so a value that is really
    an amount is never scored as a rate."""
    if raw is None or isinstance(raw, list):
        return None
    s = str(raw)
    if "%" not in s:
        return None
    m = re.search(r"(\d[\d.,]*)\s*%", s)
    if not m:
        return None
    val = _to_decimal(m.group(1).rstrip(".,"))
    return money_to_str(val) if val is not None else None


@parser("cord_text")
def cord_text(raw, *, key: str = "", **_) -> Optional[str]:
    """Item names. A list means CORD read the name as two lines; join them, because the row
    still names one product and dropping it would remove a scoreable row.

    Case is PRESERVED, matching fatura_parsers.passthrough_strip. Ground truth stores the
    printed form and core.normalize.normalise_string case-folds both sides at COMPARE time --
    casefolding here instead would deviate from the invoice convention for no gain and would
    render every item name lowercased in the review export.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        raw = " ".join(str(x) for x in raw if x is not None)
    s = re.sub(r"\s+", " ", str(raw)).strip()
    return s or None
