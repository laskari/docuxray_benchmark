"""Tests for the SROIE adapter."""
from __future__ import annotations

import pathlib
import pytest

import doctypes
from datasets.sroie_receipt import SroieAdapter
from core.canonical import BenchmarkRecord

ROOT = pathlib.Path("../../Benchmark/sroie_data/SROIE").resolve()
pytestmark = pytest.mark.skipif(not ROOT.is_dir(), reason="SROIE not mounted")

_MAP = str(pathlib.Path(__file__).resolve().parent.parent / "mapping" / "sroie_receipt.map.yaml")


@pytest.fixture(scope="module")
def adapter():
    return SroieAdapter(str(ROOT), doctypes.get("receipt"))


def test_code_and_reviewed_map_agree(adapter):
    adapter.check_contract(_MAP)


def test_the_contract_check_actually_fails_on_drift(adapter, tmp_path):
    import yaml
    doc = yaml.safe_load(pathlib.Path(_MAP).read_text())
    doc["mappings"] = [m for m in doc["mappings"] if "company" not in (m.get("labels") or [])]
    bad = tmp_path / "drifted.yaml"
    bad.write_text(yaml.safe_dump(doc))
    with pytest.raises(AssertionError, match="company"):
        adapter.check_contract(str(bad))


def test_adapter_can_build_record(adapter, monkeypatch):
    data = {
        "key_value_pairs": {
            "company": "BOOK TA .K (TAMAN DAYA) SDN BHD",
            "date": "25/12/2018",
            "address": "NO.53 55,57 & 59, JALAN SAGU 18, TAMAN DAYA, 81100 JOHOR BAHRU, JOHOR.",
            "total": "9.00"
        }
    }
    r = adapter.build("000", data)
    assert isinstance(r, BenchmarkRecord)
    assert r.gt["parties.seller.name"] == "BOOK TA .K (TAMAN DAYA) SDN BHD"
    assert r.gt["receiptInfo.txnDate"] == "25/12/2018"
    assert r.gt["totals.totalIncludingTax"] == "9.00"
    # txnDateISO is annotated under map contract 1.13: "25/12/2018" has a day > 12, so its
    # order is proved and the day-first reading is not an assumption. Before 1.13 this field
    # was excluded here as "unparseable" and the set below had four members.
    assert r.gt["receiptInfo.txnDateISO"] == "2018-12-25"
    assert r.annotated_fields == {
        "parties.seller.name",
        "receiptInfo.txnDate",
        "receiptInfo.txnDateISO",
        "parties.seller.addressStructured",
        "totals.totalIncludingTax",
    }


# --------------------------------------------------------------------------------------
# Map contract 1.13 -- txnDateISO on SROIE's own, day-first date parser
# --------------------------------------------------------------------------------------

from datasets.sroie_parsers import sroie_date_to_iso  # noqa: E402


@pytest.mark.parametrize("printed,iso", [
    ("25/12/2018", "2018-12-25"),      # day > 12 proves the order
    ("18/03/18",   "2018-03-18"),
    ("23-01-2019", "2019-01-23"),
    ("05 MAR 2018", "2018-03-05"),     # spelled month cannot be ambiguous
    ("28 MAR 18",  "2018-03-28"),
    ("24-MAR-2018", "2018-03-24"),     # the only shape FATURA's list ever parsed
    ("02/JAN/2017", "2017-01-02"),
    ("OCT 3, 2016", "2016-10-03"),
    ("2018-03-23", "2018-03-23"),      # year-first is unambiguous
    ("2018/02/22", "2018-02-22"),
    ("20180304",   "2018-03-04"),
    ("17/1/2018",  "2018-01-17"),
])
def test_1_13_parses_what_proves_its_own_order(printed, iso):
    assert sroie_date_to_iso(printed) == iso


@pytest.mark.parametrize("printed,iso", [
    ("12/28/2017", "2017-12-28"),      # doc 013 -- 28 cannot be a month
    ("12/13/2016", "2016-12-13"),      # doc 383 -- 13 cannot be a month
])
def test_1_13_1_a_page_that_proves_month_first_is_believed(printed, iso):
    """Contract 1.13.1. The corpus convention fills the absence of evidence; it never
    overrides evidence. These two documents contradict day-first and are read as printed --
    under contract 1.13 they were recorded as 'unparseable', which reported a measurement as
    a defect."""
    assert sroie_date_to_iso(printed) == iso


@pytest.mark.parametrize("printed,iso", [
    ("05/03/2018", "2018-03-05"),
    ("12-01-19",   "2019-01-12"),
    ("11.02.18",   "2018-02-11"),
    ("(06/12/2016)", "2016-12-06"),
    ("5/3/2018",   "2018-03-05"),
])
def test_1_13_1_the_open_dates_take_the_measured_convention(printed, iso):
    """Contract 1.13.1. Both leading fields <= 12, so the page settles nothing and the CORPUS
    decides: 337 of the 339 dates whose order the page does settle are day-first (99.41%), and
    129 of these 215 sit in a merchant cluster whose other receipts prove day-first while not
    one cluster proves month-first. Contract 1.13 excluded all 215; that was measurable and
    should have been measured."""
    assert sroie_date_to_iso(printed) == iso


@pytest.mark.parametrize("printed", ["25032018", "13/13/2018"])
def test_1_13_1_still_declines_what_has_no_reading(printed):
    """No separator, or neither field able to be a month. A convention cannot rescue these."""
    assert sroie_date_to_iso(printed) is None


def test_1_13_1_the_order_evidence_the_contract_rests_on(adapter):
    """The measurement itself, asserted so the contract cannot drift from its own evidence."""
    import re
    triple = re.compile(r"^(\d{1,2})([/\-.])(\d{1,2})\2(\d{2}|\d{4})$")
    day_first = month_first = 0
    for _name, data in adapter.iter_raw():
        printed = (data["key_value_pairs"].get("date") or "").strip().strip("()")
        m = triple.match(printed)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(3))
        if a > 12 and b <= 12:
            day_first += 1
        elif b > 12 and a <= 12:
            month_first += 1
    assert (day_first, month_first) == (337, 2)


def test_1_13_1_the_page_can_overrule_the_convention(adapter):
    """Rule 3b. Doc 529 prints '6/1/2018' in the header and 'JUN/2018' further down, so the
    page settles an order the numeric field leaves open -- and settles it AGAINST the corpus
    convention. This is the one document in the 215 that the convention alone gets wrong, and
    it is exactly the ~1 the contract's own error estimate predicted."""
    from datasets.sroie_parsers import resolve_date_with_page, sroie_date_to_iso
    page = ["INVOICE DATE: 6/1/2018 6:42:02 PM", "JUN/2018"]
    assert sroie_date_to_iso("6/1/2018") == "2018-01-06"          # convention alone
    assert resolve_date_with_page("6/1/2018", page) == "2018-06-01"   # page overrules it


@pytest.mark.parametrize("line", ["99 SPEED MART S/B (519537-X)",
                                  "MAKASSAR FRESH MARKET SDN BHD",
                                  "BROADVIEW MARKETING SDN BHD(728384-M)"])
def test_1_13_1_mart_and_market_are_not_march(line):
    """All three begin with MAR. A prefix test reads five day-first receipts as March, so the
    month must match as a WHOLE word and must sit beside a number."""
    from datasets.sroie_parsers import months_named_on_page
    assert months_named_on_page([line]) == set()


def test_1_13_1_a_full_spelled_date_corroborates_day_first(adapter):
    """Docs 500/503/504 print the date twice. Here the page AGREES with the convention, which
    is the other half of rule 3b being evidence rather than a thumb on the scale."""
    from datasets.sroie_parsers import resolve_date_with_page
    page = ["PRINT TIME: MONDAY, 4 DECEMBER, 2017 1:55:54 PM"]
    assert resolve_date_with_page("04/12/2017", page) == "2017-12-04"


def test_1_13_1_coverage_over_the_whole_corpus(adapter):
    """The number the map publishes: 625 scoreable, 1 unreadable. Was 408 / 215 / 3."""
    parsed = excluded = 0
    for _name, data in adapter.iter_raw():
        printed = (data["key_value_pairs"].get("date") or "").strip().strip("()")
        if sroie_date_to_iso(printed):
            parsed += 1
        else:
            excluded += 1
    assert (parsed, excluded) == (625, 1)


def test_1_13_falls_back_to_the_shared_parsers():
    """SROIE's registry must not shadow the invoice table it borrows from."""
    from datasets import sroie_parsers as P
    assert P.get("passthrough_strip")("  a  b ") == "a b"
    assert P.get("money_amount")("9.00") == "9.00"
