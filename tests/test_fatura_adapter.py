"""Adapter invariants and the three-state null semantics.

These use synthetic annotation dicts so they run without the dataset. test_corpus.py exercises
the real 10,000 files.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from datasets.fatura_invoice import (
    FaturaAdapter, check_against_yaml, check_parsers_exist, keyset_id, resolve_roles,
)
from core.canonical import BenchmarkRecord
from core.normalize import ABSENT

ROOT = pathlib.Path(__file__).resolve().parent.parent
YAML = ROOT / "mapping" / "fatura_invoice.map.yaml"


# ------------------------------------------------------------------ the contract check

def test_code_matches_the_reviewed_map():
    check_against_yaml(str(YAML))


def test_rule_table_parsers_all_exist():
    check_parsers_exist()


# ------------------------------------------------------------------ party role resolution

@pytest.mark.parametrize("labels,expected_roles,collides", [
    ({"SELLER", "BUYER"},            {"SELLER": "seller", "BUYER": "customer"},   False),
    ({"SELLER", "BILL_TO"},          {"SELLER": "seller", "BILL_TO": "customer"}, False),
    ({"SELLER", "SEND_TO"},          {"SELLER": "seller", "SEND_TO": "shipTo"},   False),
    ({"BUYER", "SEND_TO"},           {"BUYER": "customer", "SEND_TO": "shipTo"},  False),
    ({"BILL_TO", "SEND_TO"},         {"BILL_TO": "customer", "SEND_TO": "shipTo"},False),
    ({"BUYER", "BILL_TO"},           {},                                          True),
])
def test_resolve_roles(labels, expected_roles, collides):
    roles, collision = resolve_roles(labels)
    assert roles == expected_roles
    assert bool(collision) is collides


def test_billto_only_stands_in_for_buyer():
    roles, _ = resolve_roles({"BILL_TO"})
    assert roles == {"BILL_TO": "customer"}


# ------------------------------------------------------------------ record building

class _Adapter(FaturaAdapter):
    """Adapter over an in-memory corpus, so tests need no dataset on disk."""
    def __init__(self, corpus):
        self._corpus = corpus
        self.root = pathlib.Path("/nonexistent")
        self.ann_dir = self.root / "modified_annotations"
        self.img_dir = self.root / "images"
        self.doc_type = "invoice"
        self._template_vocab = {}
        self.stats = {}

    def _iter_raw(self):
        return iter(self._corpus.items())


def build(corpus, stem):
    a = _Adapter(corpus)
    a.build_template_vocab()
    return a.build(stem, corpus[stem])


BASE = {"Image_Name": "x.jpg", "SELLER": {"Name": "Acme", "Address": "1 A St\nB, CA 90001 US",
                                          "Email": "s@acme.test", "Site": "acme.test"}}


def test_state_i_present_value():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "734.33 EUR"}}
    r = build(corpus, "T1_Instance0")
    assert r.gt["totals.totalIncludingTax"] == "734.33"
    assert r.gt["currency"] == "EUR"
    assert "totals.totalIncludingTax" in r.present_fields


def test_state_ii_absent_when_template_vocab_has_the_label():
    corpus = {
        "T1_Instance0": {**BASE, "DUE_DATE": "16-Oct-2016"},
        "T1_Instance1": {**BASE},                       # same template, no DUE_DATE
    }
    r = build(corpus, "T1_Instance1")
    assert r.gt["invoiceInfo.dueDate"] is ABSENT
    assert "invoiceInfo.dueDate" in r.absent_fields
    assert "invoiceInfo.dueDate" not in r.present_fields


def test_state_iii_never_annotated_is_absent_from_the_record_entirely():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "10.00 EUR"}}
    r = build(corpus, "T1_Instance0")
    # No PO_NUMBER anywhere in this template's vocabulary -> not scoreable either way.
    assert "invoiceInfo.purchaseOrderNumber" not in r.annotated_fields
    assert "invoiceInfo.purchaseOrderNumber" not in r.gt


def test_null_subkey_is_a_scoreable_absence_not_an_exclusion():
    corpus = {"T1_Instance0": {**BASE, "SELLER": {**BASE["SELLER"], "Name": None}}}
    r = build(corpus, "T1_Instance0")
    assert r.gt["parties.seller.name"] is ABSENT
    assert "parties.seller.name" not in r.excluded_fields


def test_seller_phone_is_never_generated():
    # FATURA has no SELLER_TEL label, so parties.seller.phone is not_annotated -- it must not
    # show up as an unparseable miss.
    r = build({"T1_Instance0": dict(BASE)}, "T1_Instance0")
    assert "parties.seller.phone" not in r.annotated_fields
    assert "parties.seller.phone" not in r.excluded_fields


def test_seller_site_is_unmapped_schema_gap():
    r = build({"T1_Instance0": dict(BASE)}, "T1_Instance0")
    assert not any("website" in p or p.endswith(".site") for p in r.annotated_fields)


# ------------------------------------------------------------------ dedupe and conflict

def test_amount_due_and_total_agree_and_are_deduplicated():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "734.33 EUR", "AMOUNT_DUE": "734.33 EUR"}}
    r = build(corpus, "T1_Instance0")
    assert r.gt["totals.totalIncludingTax"] == "734.33"


def test_conflicting_labels_on_one_path_are_excluded_not_guessed():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "734.33 EUR", "AMOUNT_DUE": "999.99 EUR"}}
    r = build(corpus, "T1_Instance0")
    p = "totals.totalIncludingTax"
    assert p in r.excluded_fields and "conflicting" in r.excluded_fields[p]
    assert p not in r.gt


# ------------------------------------------------------------------ structural exclusions

def test_multi_rate_gst_excludes_tax_paths_as_a_schema_gap():
    corpus = {"T1_Instance0": {**BASE, "GST(18%)": "230.72", "GST(5%)": "23.16"}}
    r = build(corpus, "T1_Instance0")
    for p in ("totals.taxName", "totals.taxAmount",
              "totals.taxPercentage"):
        assert "SCHEMA GAP" in r.excluded_fields[p]


def test_single_gst_maps_cleanly():
    corpus = {"T1_Instance0": {**BASE, "GST(18%)": "230.72"}}
    r = build(corpus, "T1_Instance0")
    assert r.gt["totals.taxName"] == "GST"
    assert r.gt["totals.taxPercentage"] == "18.00"
    assert r.gt["totals.taxAmount"] == "230.72"


def test_tax_and_gst_together_are_excluded():
    corpus = {"T1_Instance0": {**BASE, "TAX": "VAT (3%): 3.00 EUR", "GST(18%)": "230.72"}}
    r = build(corpus, "T1_Instance0")
    assert "totals.taxAmount" in r.excluded_fields


def test_buyer_billto_collision_excludes_customer():
    corpus = {"T1_Instance0": {**BASE,
                               "BUYER": {"Name": "A", "Address": "x", "Tel": "1", "Email": "a@b.c", "Site": "s"},
                               "BILL_TO": {"Name": "B", "Address": "y", "Tel": "2", "Email": "b@b.c", "Site": "s"}}}
    r = build(corpus, "T1_Instance0")
    assert "parties.customer.name" in r.excluded_fields
    assert "BUYER and BILL_TO" in r.excluded_fields["parties.customer.name"]


def test_seller_buyer_duplicate_is_flagged_not_excluded():
    shared = "1 A St\nB, CA 90001 US"
    corpus = {"T1_Instance0": {**BASE,
                               "SELLER": {**BASE["SELLER"], "Address": shared, "Email": "same@x.test"},
                               "BUYER": {"Name": "A", "Address": shared, "Tel": "1",
                                         "Email": "same@x.test", "Site": "s"}}}
    r = build(corpus, "T1_Instance0")
    # The page genuinely prints the same address and email in both blocks (verified against
    # images/Template1_Instance0.jpg), so the field is scored, not dropped -- only flagged.
    assert "parties.seller.addressStructured" not in r.excluded_fields
    assert "parties.seller.email" not in r.excluded_fields
    assert r.meta["seller_equals_buyer_address"] is True
    assert r.meta["seller_equals_buyer_email"] is True
    assert r.gt["parties.seller.addressStructured"] is not None


def test_ambiguous_dollar_excludes_currency():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "266.80 $"}}
    r = build(corpus, "T1_Instance0")
    assert "ambiguous" in r.excluded_fields["currency"]
    assert "currency" not in r.gt


def test_unambiguous_evidence_wins_over_dollar():
    corpus = {"T1_Instance0": {**BASE, "TOTAL": "266.80 $", "SUB_TOTAL": "250.00 EUR"}}
    r = build(corpus, "T1_Instance0")
    assert r.gt["currency"] == "EUR"


def test_known_gt_error_documents_are_excluded():
    corpus = {"Template18_Instance4": {**BASE, "TOTAL": "DUE_AMOUNT : 240.38 $"}}
    a = _Adapter(corpus); a.build_template_vocab()
    r = a.build("Template18_Instance4", corpus["Template18_Instance4"])
    assert "totals.totalIncludingTax" in r.excluded_fields


# ------------------------------------------------------------------ line items

def test_line_items_are_captured_but_never_scoreable():
    """FATURA line-item GT recovers ~29% of printed rows and the 'ok' flag is unreliable
    (Template4_Instance0 is 'ok' with 1 row against 4 on the page). It is research data only."""
    rows = [{"description": "Widget", "quantity": "4.00", "price": "$93.88"}]
    ok = {"T1_Instance0": {**BASE, "LINE_ITEMS": rows, "_line_items_status": "ok"}}
    r = build(ok, "T1_Instance0")
    assert r.meta["gt_line_items"] == [{"description": "Widget", "quantity": "4.00",
                                        "unitPrice": "93.88"}]
    assert "lineItems" not in r.gt
    assert not any(p.startswith("lineItems") for p in r.annotated_fields)


def test_no_line_item_path_is_ever_scoreable():
    # An unrecovered row is a parser failure in fatura_to_keyvalue.py, not evidence the page
    # had no rows -- so line items must never appear as a scoreable absence.
    corpus = {
        "T1_Instance0": {**BASE, "LINE_ITEMS": [{"description": "W", "quantity": "1", "price": "1"}],
                         "_line_items_status": "ok"},
        "T1_Instance1": {**BASE, "_line_items_status": "no_header_found"},
    }
    for stem in ("T1_Instance0", "T1_Instance1"):
        r = build(corpus, stem)
        assert "lineItems" not in r.gt
        assert not any(p.startswith("lineItems") for p in r.annotated_fields)


# ------------------------------------------------------------------ record invariants

def test_validate_rejects_gt_outside_annotated_fields():
    r = BenchmarkRecord(doc_id="d", source_dataset="x", doc_type="invoice", image_path="i",
                        cluster_id="T1", keyset_id="k", gt={"a": "1"}, annotated_fields=set())
    with pytest.raises(ValueError, match="not in annotated_fields"):
        r.validate()


def test_validate_rejects_annotated_and_excluded_overlap():
    r = BenchmarkRecord(doc_id="d", source_dataset="x", doc_type="invoice", image_path="i",
                        cluster_id="T1", keyset_id="k", gt={"a": "1"},
                        annotated_fields={"a"}, excluded_fields={"a": "why"})
    with pytest.raises(ValueError, match="both annotated and excluded"):
        r.validate()


def test_validate_requires_cluster_id():
    r = BenchmarkRecord(doc_id="d", source_dataset="x", doc_type="invoice", image_path="i",
                        cluster_id="", keyset_id="k")
    with pytest.raises(ValueError, match="cluster_id"):
        r.validate()


def test_jsonl_roundtrip_preserves_the_absent_sentinel():
    corpus = {
        "T1_Instance0": {**BASE, "DUE_DATE": "16-Oct-2016", "TOTAL": "10.00 EUR"},
        "T1_Instance1": {**BASE, "TOTAL": "11.00 EUR"},
    }
    r = build(corpus, "T1_Instance1")
    back = BenchmarkRecord.from_json(r.to_json())
    assert back.gt["invoiceInfo.dueDate"] is ABSENT
    assert back.present_fields == r.present_fields
    assert back.absent_fields == r.absent_fields
    assert back.annotated_fields == r.annotated_fields


def test_keyset_id_is_order_independent_and_stable():
    assert keyset_id(["B", "A"]) == keyset_id(["A", "B"])
    assert keyset_id(["A"]) != keyset_id(["A", "B"])
