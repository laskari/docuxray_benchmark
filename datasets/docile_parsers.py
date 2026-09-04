"""DocILE100 label parsers.

DocILE100 values are mostly atomic — one label, one schema value — so these are thinner than
the FATURA parsers, which had to split composite printed strings. What DocILE100 needs instead
is honest handling of DATE AMBIGUITY, because unlike FATURA it has no single date format.

A parser returns a canonical value (str for text/date/identifier, str-of-2dp-Decimal for
numerics) or None when the input carries no such component. Parsers never raise on ordinary
input: an unparseable value returns None and the adapter records why, so one odd document
cannot abort a build.
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import re
import sys
from typing import Callable, Dict, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import (  # noqa: E402
    money_to_str,
    normalise_money,
    normalise_string,
)

Parser = Callable[..., Optional[str]]
_REGISTRY: Dict[str, Parser] = {}


def parser(name: str) -> Callable[[Parser], Parser]:
    def register(fn: Parser) -> Parser:
        _REGISTRY[name] = fn
        return fn
    return register


def get(name: str) -> Parser:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown parser {name!r}; known: {sorted(_REGISTRY)}") from None


def known() -> list:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- dates
# Every format actually observed across the 95 dated documents, most frequent first. Listed
# explicitly rather than handed to a fuzzy date library: a library that silently resolves
# "06/07/01" is exactly the behaviour this dataset must NOT have.
_UNAMBIGUOUS_FORMATS = (
    "%B %d, %Y",     # May 29, 2022                   (18)
    "%b %d, %Y",     # May 29, 2022 (abbreviated)
    "%d-%b-%Y",      # 04-APR-1999
    "%d-%b-%y",      # 06-SEP-01
    "%b%d/%y",       # SEP30/20, AUG12/02
    "%b%d/%Y",
)
# Numeric forms where the first two components could be either order.
_NUMERIC_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$")
# A GT value carrying a time component. issueDate's own description forbids a time, so such a
# value is a ground-truth defect, not something to parse around.
_HAS_TIME_RE = re.compile(r"\d{1,2}:\d{2}(:\d{2})?|\b\d{1,2}\.\d{2}\b")
# A trailing meridiem survives time-stripping and then defeats every date format, which is how
# '8/4/2020 12:50:24 PM' produced no ISO and no recorded reason for it — the one outcome this
# classifier exists to prevent.
_MERIDIEM_RE = re.compile(r"\s*\b[AaPp]\.?[Mm]\.?\b\s*$")

AMBIGUOUS_DATE = "__AMBIGUOUS_DATE__"
"""Both candidate readings are valid calendar dates and they disagree. The adapter records an
exclusion rather than picking one — DocILE is US-sourced, so M/D is LIKELY, and likely is not
ground truth."""

GT_DEFECT_DATE = "__GT_DEFECT_DATE__"
"""The label value violates the schema field's own contract (an embedded time)."""


def _four_digit_year(y: int, raw2: str) -> int:
    if len(raw2) == 4:
        return y
    # 2-digit year. DocILE spans the 1990s to the 2020s, so pivot on the corpus, not on
    # POSIX's 1969 rule: 00-30 -> 2000s, 31-99 -> 1900s.
    return 2000 + y if y <= 30 else 1900 + y


def _strip_clock(s: str) -> str:
    """Remove a time component and any trailing meridiem."""
    return _MERIDIEM_RE.sub("", _HAS_TIME_RE.sub("", s)).strip().rstrip(",").strip()


def classify_date(raw) -> str | None:
    """AMBIGUOUS_DATE / GT_DEFECT_DATE / None (parseable or simply empty).

    Separate from the parser so the adapter can record WHY a document lost its ISO target
    instead of just seeing a None it cannot explain.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()

    m = _NUMERIC_RE.match(s)
    if m is None and _HAS_TIME_RE.search(s):
        # A time on a non-numeric form (e.g. "SEP30/20 13.06") is stripped by the parser
        # below; the defect that matters is a time on a value that is otherwise a clean date.
        # GT_DEFECT wins over AMBIGUOUS when both apply: "the annotation breaks the field's own
        # contract" is the more specific and more actionable statement.
        if _NUMERIC_RE.match(_strip_clock(s)):
            return GT_DEFECT_DATE
        return None
    if m is None:
        return None

    a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if a <= 12 and b <= 12 and a != b:
        return AMBIGUOUS_DATE          # a==b reads the same either way, so it is not ambiguous
    return None


@parser("date_to_iso")
def date_to_iso(raw, *, key: str = "", **_) -> Optional[str]:
    """ISO-8601, or None when the value cannot be resolved WITHOUT GUESSING.

    Returns None for anything classify_date flags. That is deliberate: silently choosing M/D
    would score 23 of 95 documents on an assumption about a dataset whose locale is nowhere
    stated.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    if classify_date(raw) is not None:
        return None

    # Trailing clock noise on the SEP30/20-style forms.
    s_clean = _strip_clock(raw.strip())

    for fmt in _UNAMBIGUOUS_FORMATS:
        try:
            return _dt.datetime.strptime(s_clean, fmt).date().isoformat()
        except ValueError:
            continue

    m = _NUMERIC_RE.match(s_clean)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        month, day = (a, b) if a <= 12 else (b, a)      # unambiguous by construction here
        try:
            return _dt.date(_four_digit_year(int(y), y), month, day).isoformat()
        except ValueError:
            return None
    return None


@parser("passthrough_strip")
def passthrough_strip(raw, *, key: str = "", **_) -> Optional[str]:
    return normalise_string(raw) if isinstance(raw, str) else None


@parser("money_amount")
def money_amount(raw, *, key: str = "", **_) -> Optional[str]:
    """'$5.00', '73,931.00', '.00', 16.45 -> a 2dp string.

    Accepts a bare number as well as a string: itemCaseSize and some quantities arrive as JSON
    numbers, and refusing those would silently drop real values.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return money_to_str(normalise_money(str(raw)))
    if not isinstance(raw, str) or not raw.strip():
        return None
    return money_to_str(normalise_money(raw))
