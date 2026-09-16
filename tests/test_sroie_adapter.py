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
    assert r.annotated_fields == {
        "parties.seller.name",
        "receiptInfo.txnDate",
        "parties.seller.addressStructured",
        "totals.totalIncludingTax",
    }
