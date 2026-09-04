"""FATURA label parsers.

FATURA values are composite printed strings, not atomic fields: one GT label can carry a tax
name, a rate, an amount and a currency at once. Each parser below extracts ONE schema-path
value from a raw GT value, so a map row with four targets calls four parsers over the same
input. Parsers are pure and registered by name; fatura_field_map.yaml references them by name.

A parser returns:
  * a canonical value (str for text/date/identifier, str-of-2dp-Decimal for numerics)
  * None when the input carries no such component

Parsers never raise on ordinary input -- an unparseable value returns None and the adapter
records it, so one odd document cannot abort a 10,000-file build.
"""
from __future__ import annotations

import re
from typing import Callable, Dict, Optional

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import (  # noqa: E402
    AMBIGUOUS_CURRENCY,
    money_to_str,
    normalise_address,
    normalise_currency,
    normalise_date,
    normalise_money,
    normalise_percent,
    normalise_phone,
    normalise_string,
    strip_label_prefix,
)

# FATURA renders dates uniformly as DD-Mon-YYYY. Recorded here rather than guessed per
# document -- see fatura_field_map.yaml, DATE row.
FATURA_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y")

Parser = Callable[..., Optional[str]]
_REGISTRY: Dict[str, Parser] = {}


def parser(name: str) -> Callable[[Parser], Parser]:
    def register(fn: Parser) -> Parser:
        _REGISTRY[name] = fn
        fn.parser_name = name  # type: ignore[attr-defined]
        return fn
    return register


def get(name: str) -> Parser:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown parser {name!r}; fatura_field_map.yaml references a parser that does "
            f"not exist. Known: {sorted(_REGISTRY)}"
        ) from None


def known() -> list:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------

@parser("passthrough_strip")
def passthrough_strip(raw, *, key: str = "", **_) -> Optional[str]:
    """Trim and collapse whitespace, preserving case. For names, identifiers, emails."""
    if raw is None:
        return None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    return s or None


@parser("literal")
def literal(raw, *, value: str = "", **_) -> Optional[str]:
    """Emit a fixed value from the map row (used by GST(n%), whose tax name is implicit)."""
    return value or None


@parser("address_whole")
def address_whole(raw, *, key: str = "", **_) -> Optional[str]:
    """Flatten a multi-line address block to one comma-separated normalised line."""
    return normalise_address(raw)


@parser("phone_normalise")
def phone_normalise(raw, *, key: str = "", **_) -> Optional[str]:
    return normalise_phone(raw)


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------

@parser("date_to_iso")
def date_to_iso(raw, *, key: str = "", **_) -> Optional[str]:
    """'20-Mar-2008' -> '2008-03-20'. Strict to FATURA's own format.

    A value that does not parse returns None rather than being coerced -- the two known
    label-swapped dates (Template38_Instance128, Template40_Instance58) are handled as
    exclusions in known_gt_errors.json, not silently reinterpreted here.
    """
    return normalise_date(raw, formats=FATURA_DATE_FORMATS)


# --------------------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------------------

@parser("money_amount")
def money_amount(raw, *, key: str = "", **_) -> Optional[str]:
    """'734.33 EUR' -> '734.33'.  'BALANCE_DUE : 481.84 $' -> '481.84'."""
    return money_to_str(normalise_money(raw, which="last"))


@parser("money_abs")
def money_abs(raw, *, key: str = "", **_) -> Optional[str]:
    """Positive magnitude. '(1.85%): (-) 13.42' -> '13.42'.

    GT discount is stored as a positive magnitude because
    ai/postprocessing/invoice_postprocessor.py::_normalize_totals converts a negative
    discountTotal to its absolute value. Storing GT negative would make the postprocessed
    arm score worse than bare extraction on every discounted invoice as a pure sign artifact. See fatura_field_map.yaml.
    """
    value = normalise_money(raw, which="last")
    return money_to_str(abs(value)) if value is not None else None


@parser("money_currency")
def money_currency(raw, *, key: str = "", **_) -> Optional[str]:
    """'725.30 EUR' -> 'EUR'.  '266.80 $' -> AMBIGUOUS sentinel (caller excludes the field)."""
    return normalise_currency(raw)


@parser("pct_in_parens")
def pct_in_parens(raw, *, key: str = "", **_) -> Optional[str]:
    """'(1.85%): (-) 13.42' -> '1.85'."""
    value = normalise_percent(raw)
    return money_to_str(value) if value is not None else None


# --------------------------------------------------------------------------------------
# Tax -- one label, four schema paths
# --------------------------------------------------------------------------------------

# 'VAT (3.88%): 28.18 EUR'  ->  name 'VAT', rate 3.88, amount 28.18, currency EUR
_TAX_NAME_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .&/-]*?)\s*(?=[(:]|$)")


@parser("tax_name")
def tax_name(raw, *, key: str = "", **_) -> Optional[str]:
    """Leading label before the rate or the colon. 'VAT (3.88%): 28.18 EUR' -> 'VAT'."""
    if raw is None:
        return None
    m = _TAX_NAME_RE.match(strip_label_prefix(str(raw)))
    if not m:
        return None
    name = m.group(1).strip(" .-")
    return name or None


@parser("tax_pct")
def tax_pct(raw, *, key: str = "", **_) -> Optional[str]:
    """'VAT (3.88%): 28.18 EUR' -> '3.88'."""
    value = normalise_percent(raw)
    return money_to_str(value) if value is not None else None


@parser("tax_amount")
def tax_amount(raw, *, key: str = "", **_) -> Optional[str]:
    """The amount AFTER the colon, so the rate inside the parens is never mistaken for it.

    'VAT (3.88%): 28.18 EUR' -> '28.18'
    """
    if raw is None:
        return None
    s = strip_label_prefix(str(raw))
    tail = s.split(":", 1)[1] if ":" in s else s
    # Belt and braces: drop any parenthesised group so a rate can never leak through.
    tail = re.sub(r"\([^)]*\)", " ", tail)
    return money_to_str(normalise_money(tail, which="last"))


_GST_KEY_RE = re.compile(r"^\s*GST\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)\s*$", re.IGNORECASE)


@parser("rate_from_key")
def rate_from_key(raw, *, key: str = "", **_) -> Optional[str]:
    """The rate lives in the LABEL, not the value: key 'GST(18%)' -> '18.00'.

    200 files carry five simultaneous GST rates; InvoiceData.Totals is scalar and cannot hold
    them. Those files are excluded structurally by the adapter, not here.
    """
    m = _GST_KEY_RE.match(str(key or ""))
    if not m:
        return None
    from decimal import Decimal
    return money_to_str(Decimal(m.group(1)))


__all__ = [
    "get", "known", "parser", "Parser",
    "AMBIGUOUS_CURRENCY", "FATURA_DATE_FORMATS",
]
