"""SROIE value parsers.

One parser lives here, and it exists because of a defect the frozen map carried from the day
SROIE was wired in: `receiptInfo.txnDateISO` was built with `fatura_parsers.date_to_iso`, whose
format list is `("%d-%b-%Y", "%d-%B-%Y")` and is documented as strict to FATURA's own rendering.
SROIE prints dates seventeen ways and only five of its 626 documents are `24-MAR-2018`, so 621
were recorded as "GT value present but unparseable (date)" and the field was reported at n=5 --
a headline-ineligible row that looked like a coverage fact about the corpus when it was a fact
about the parser.

WHICH DAY/MONTH ORDER. `core.normalize.normalise_date` refuses to guess, by design: "the
ambiguity is resolved per-dataset in the adapter and recorded there, not guessed per-document."
This module resolves it for SROIE and records it here:

    SROIE is a Malaysian corpus (ICDAR 2019 Task 3; every receipt in it is Malaysian, priced in
    RM and addressed in Johor, Selangor or KL). Malaysia writes DD/MM/YYYY. So day-first is the
    correct reading, not a coin toss.

WHICH ORDER, MEASURED RATHER THAN ASSERTED. Contract 1.13 originally declined every numeric
date whose two leading fields are both <= 12 ("05/03/2018"), on the ground that day-first was
an assumption the page did not confirm. That was the cautious reading, but it left 215 of 626
documents unscored on a question the CORPUS can answer. Counting only the dates whose printed
form settles their own order:

    PROVED day-first   (first field > 12)      337
    PROVED month-first (second field > 12)       2      docs 013 '12/28/2017', 383 '12/13/2016'
    ----------------------------------------------
    day-first share of the settled cases    99.41%

That is not a locale argument, it is a measurement on this corpus, and it is corroborated one
level down: of the 215 ambiguous dates, 129 sit in a MERCHANT CLUSTER (documents sharing a
printed header) whose OTHER receipts prove day-first, and NOT ONE cluster proves month-first.
The two month-first documents are one-off merchants with no sibling receipts, so applying the
convention cannot mis-read a second date from either of them.

So the order is resolved in this priority, strongest evidence first:

    1  second field > 12   -> PROVED month-first, parsed month-first     12/28/2017
    2  first field > 12    -> PROVED day-first, parsed day-first         25/12/2018
    3  spelled month or leading four-digit year -> order self-evident    05 MAR 2018, 2018-03-23
    3b a spelled month ELSEWHERE ON THE PAGE that fits exactly one of the two readings
                            -> that reading                              6/1/2018 + "JUN/2018"
    4  both fields <= 12 and nothing above applies -> the corpus convention, day-first

Rule 3b is why `resolve_date_with_page` exists. Many receipts print the date twice, once
numerically in the header and once in full lower down ("SATURDAY, 2 DECEMBER, 2017", the
month-and-year stamp "JUN/2018"), and where they do, the PAGE settles an order the numeric
field leaves open -- in either direction. Measured over the 215 open dates: 3 corroborate
day-first (500, 503, 504) and 1 corroborates month-first (529). The month token must be a
whole word -- a bare three-letter abbreviation or a full month name -- because '99 SPEED
MART', 'FRESH MARKET' and 'BROADVIEW MARKETING' all start with MAR and a prefix test reads
five day-first receipts as March.

Only rule 4 is an inference, and it is quantified: given that a month-first date is detectable
only when its day exceeds 12 (about 61% of the time), 2 detected month-first documents imply
roughly 3 in the corpus and therefore about ONE undetected in the ambiguous set -- and rule 3b
has since found and fixed exactly that one, doc 529. What remains under rule 4 alone is 211
dates with no evidence either way.

Measured over all 626 SROIE annotations: 625 parse, 1 is excluded as unreadable ('25032018'
carries no separator). Against the 5 the FATURA format list allowed.

Registered in this module's OWN registry, with get() falling back to fatura_parsers, so
`fatura_parsers._REGISTRY` is untouched. Same pattern as cord_parsers.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable, Dict, Optional

from datasets import fatura_parsers as _fatura

Parser = Callable[..., Optional[str]]
_REGISTRY: Dict[str, Parser] = {}


def parser(name: str) -> Callable[[Parser], Parser]:
    def register(fn: Parser) -> Parser:
        _REGISTRY[name] = fn
        fn.parser_name = name       # type: ignore[attr-defined]
        return fn
    return register


def get(name: str) -> Parser:
    """SROIE's parsers first, then the shared invoice ones."""
    if name in _REGISTRY:
        return _REGISTRY[name]
    return _fatura.get(name)


def known() -> list:
    return sorted(set(_REGISTRY) | set(_fatura.known()))


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------

#: Day-first numeric forms -- the corpus convention (99.41% of the settled cases).
_DAY_FIRST = ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y")

#: Month-first numeric forms. Used ONLY where the second field exceeds 12 and the page has
#: therefore proved month-first against the corpus convention -- 2 documents. Never a guess.
_MONTH_FIRST = ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%m.%d.%Y", "%m.%d.%y")

#: Forms that cannot be ambiguous: a spelled month, or a four-digit year in front.
_UNAMBIGUOUS = ("%d %b %Y", "%d %b %y", "%d-%b-%Y", "%d-%b-%y", "%d/%b/%Y", "%d/%b/%y",
                "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%d-%B-%Y",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d")

#: A bare numeric pair plus a year: '25/12/2018', '12-01-19', '11.02.18'.
_NUMERIC_TRIPLE = re.compile(r"^(\d{1,2})([/\-.])(\d{1,2})\2(\d{2}|\d{4})$")


def _try(value: str, formats) -> Optional[str]:
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@parser("sroie_date_to_iso")
def sroie_date_to_iso(raw, *, key: str = "", **_) -> Optional[str]:
    """'25/12/2018' -> '2018-12-25'. None only when no reading is possible at all.

    Field order is resolved by the four-rule ladder in the module docstring: the page's own
    proof first, in EITHER direction, and the measured corpus convention (day-first) only for
    the dates the page leaves open. A date that proves month-first is read month-first -- the
    convention never overrides evidence, it only fills its absence.
    """
    if raw is None:
        return None
    s = re.sub(r"\s+", " ", str(raw)).strip()
    # SROIE wraps one date in parentheses: '(06/12/2016)'.
    s = s.strip("()").strip()
    if not s:
        return None

    got = _try(s, _UNAMBIGUOUS) or _try(s.upper(), _UNAMBIGUOUS)
    if got:
        return got

    m = _NUMERIC_TRIPLE.match(s)
    if not m:
        return None
    first, second = int(m.group(1)), int(m.group(3))
    if first > 12 and second > 12:
        return None                      # no reading works; neither field can be a month
    if first <= 12 < second:
        # The page contradicts the corpus convention and is believed: 13 cannot be a month.
        return _try(s, _MONTH_FIRST)
    # first > 12 (proved day-first), or both <= 12 (the measured convention). Same formats.
    return _try(s, _DAY_FIRST)


#: A month named in words. Matched as a WHOLE WORD -- the bare abbreviation or the full name --
#: never as a prefix: '99 SPEED MART', 'MAKASSAR FRESH MARKET' and 'BROADVIEW MARKETING' all
#: begin with MAR, and a prefix test reads five day-first receipts as March.
_MONTH_NAMES = {}
for _i, _abbr in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                            "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1):
    _MONTH_NAMES[_abbr] = _i
for _i, _full in enumerate(("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                            "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"), start=1):
    _MONTH_NAMES[_full] = _i
_WORD = re.compile(r"[A-Za-z]+")

#: A spelled month only counts as a DATE when a number sits beside it -- a day or a year.
_MONTH_IN_DATE = re.compile(
    r"(?:(\d{1,4})\s*[\s,/\-.]\s*([A-Za-z]+)|([A-Za-z]+)\s*[\s,/\-.]\s*(\d{1,4}))")


def months_named_on_page(ocr_lines) -> set:
    """Every month the page names IN A DATE CONTEXT, as numbers 1-12."""
    out = set()
    for line in (ocr_lines or []):
        for m in _MONTH_IN_DATE.finditer(str(line)):
            for word in (m.group(2), m.group(3)):
                if word and word.upper() in _MONTH_NAMES:
                    out.add(_MONTH_NAMES[word.upper()])
    return out


def resolve_date_with_page(raw, ocr_lines) -> Optional[str]:
    """Rule 3b, then the ladder. Returns the ISO value for one printed date.

    Consulted ONLY for a numeric date whose two leading fields are both <= 12, i.e. exactly
    where the numeric field itself settles nothing. If the page names a month in a date context
    and it fits ONE of the two readings, that reading wins -- over the corpus convention, in
    either direction. If it fits both or neither, nothing is learned and the ladder decides.
    """
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip("()").strip()
    m = _NUMERIC_TRIPLE.match(s)
    if not m:
        return sroie_date_to_iso(raw)
    first, second = int(m.group(1)), int(m.group(3))
    if first > 12 or second > 12:
        return sroie_date_to_iso(raw)          # the page's own field already proves the order
    named = months_named_on_page(ocr_lines)
    if named:
        day_first_ok, month_first_ok = second in named, first in named
        if day_first_ok != month_first_ok:
            return _try(s, _DAY_FIRST if day_first_ok else _MONTH_FIRST)
    return sroie_date_to_iso(raw)


#: Why one printed date produced no ISO value. The ORDER-AMBIGUOUS bucket that contract 1.13
#: published is gone: the corpus settles that question (see the module docstring), so a date
#: with both fields <= 12 is now read rather than excluded. What is left cannot be read at all.
IMPOSSIBLE = "GT date has no valid reading (neither leading field can be a month)"
UNREADABLE = "GT value present but unparseable (date)"


def date_exclusion_reason(raw) -> str:
    """The reason to record when sroie_date_to_iso declined a printed date."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip().strip("()").strip()
    m = _NUMERIC_TRIPLE.match(s)
    if m and int(m.group(1)) > 12 and int(m.group(3)) > 12:
        return IMPOSSIBLE
    return UNREADABLE
