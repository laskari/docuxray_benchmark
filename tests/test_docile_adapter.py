"""The DocILE100 adapter, and the two rules that make it different from FATURA's.

Both rules are consequences of the dataset, not preferences, and both are the kind of thing
that is silently wrong rather than loudly broken — so each gets a test that fails if someone
"simplifies" it later.
"""
from __future__ import annotations

import pathlib

import pytest

import doctypes
from core.normalize import _Absent
from datasets import docile_parsers as P
from datasets.docile_invoice import DocileAdapter

ROOT = pathlib.Path.home() / "mnt" / "Benchmark" / "DocILE"
pytestmark = pytest.mark.skipif(not ROOT.is_dir(), reason="DocILE100 not mounted")

_MAP = str(pathlib.Path(__file__).resolve().parent.parent
           / "mapping" / "docile_invoice.map.yaml")


@pytest.fixture(scope="module")
def adapter():
    return DocileAdapter(ROOT, doctypes.get("invoice"))


@pytest.fixture(scope="module")
def records(adapter):
    return {r.doc_id: r for r in adapter.iter_records()}


# --------------------------------------------------------------------- the contract
def test_code_and_reviewed_map_agree(adapter):
    """Gates every ground-truth build. A map edited without a code change must stop the
    build, not change the numbers."""
    adapter.check_contract(_MAP)


def test_the_contract_check_actually_fails_on_drift(adapter, tmp_path):
    """A contract check that cannot fail is decoration."""
    import yaml
    doc = yaml.safe_load(pathlib.Path(_MAP).read_text())
    doc["mappings"] = [m for m in doc["mappings"]
                       if "Vendor Information.vendorName" not in (m.get("labels") or [])]
    bad = tmp_path / "drifted.yaml"
    bad.write_text(yaml.safe_dump(doc))
    with pytest.raises(AssertionError, match="vendorName"):
        adapter.check_contract(str(bad))


# --------------------------------------------------------------------- rule 1: no absence
def test_no_field_is_ever_recorded_as_authoritatively_absent(records):
    """DocILE100 is 100 unrelated documents with 93 distinct vendors — no repeating structure
    licenses absence, so a null cannot be distinguished from an unrecorded value. Writing
    ABSENT here would manufacture a hallucination denominator out of nothing, which is exactly
    how FATURA's hallucination count turned out to be 100% artefact."""
    for rec in records.values():
        for path, value in rec.gt.items():
            assert not isinstance(value, _Absent), f"{rec.doc_id}:{path} recorded as absent"


def test_a_null_label_is_left_out_of_annotated_fields_entirely(records):
    """State (iii): not in gt AND not in annotated_fields, so it leaves every denominator.
    taxes is null on 90 of 100 documents — if those counted, the field would look measured."""
    with_tax = [r for r in records.values() if "totals.taxAmount" in r.annotated_fields]
    assert len(with_tax) == 10, "taxAmount should be annotated on exactly its 10 supporting docs"
    for r in records.values():
        assert set(r.gt) <= r.annotated_fields


# --------------------------------------------------------------------- rule 2: line items
def test_line_items_are_annotated_and_carry_their_alignment_contract(records):
    """FATURA's line items are unscoreable; DocILE100's are the point of the dataset. The
    adapter must also ship the row-alignment contract, or core/rows.py has to guess."""
    rec = records["039b024e54fe41e8ad8717e8_0"]      # the 44-row radio log
    assert "lineItems" in rec.annotated_fields
    assert len(rec.gt["lineItems"]) == 44
    assert rec.meta["row_targets"]["lineItems"]["lineTotal"] == \
        ["lineTotalExcludingTax", "lineTotalIncludingTax"]
    assert rec.meta["row_match_keys"]["lineItems"] == ["description", "descriptionAlt"]
    assert rec.meta["row_text_leaves"]["totals.otherCharges"] == ["key"]


def test_a_document_with_no_itemised_table_is_excluded_not_scored_as_empty(records):
    """004c5e8c… is a Vendor Remittance Advice — verified by eye, it has no itemised table.
    Scoring it as a correct empty table would credit the model for a table nobody asked for;
    scoring it as a miss would penalise it for the same. Excluded, with the reason published."""
    rec = records["004c5e8c990342e9882b70b1_0"]
    assert "lineItems" not in rec.annotated_fields
    assert "lineItems" in rec.excluded_fields
    assert "no itemised table" in rec.excluded_fields["lineItems"]


def test_the_drifting_description_column_is_captured_as_an_alternate(records):
    """itemName is the generic '60 Spot' on the radio-log family and the real description sits
    in itemCode. Both are carried so alignment can accept either."""
    row = records["039b024e54fe41e8ad8717e8_0"].gt["lineItems"][0]
    # normalise_string lower-cases, as it does for every text field in this harness — both
    # sides of a comparison go through it, so matching stays symmetric.
    assert row["description"] == "60 spot"
    assert row["descriptionAlt"].startswith("20afscmemtor1r")


# --------------------------------------------------------------------- dates
def test_an_ambiguous_date_loses_its_iso_twin_with_a_published_reason(records):
    """23 of 95 dates have day and month both <= 12. DocILE states no locale, so M/D vs D/M
    cannot be resolved — and resolving it by assuming US order would score a quarter of the
    field on a guess."""
    ambiguous = [r for r in records.values()
                 if "invoiceInfo.issueDateISO" in r.excluded_fields]
    assert ambiguous, "expected some documents to lose ISO to ambiguity"
    for r in ambiguous:
        assert "invoiceInfo.issueDateISO" not in r.annotated_fields
        assert "invoiceInfo.issueDate" in r.annotated_fields, (
            "raw-format fidelity is still scoreable when only the ISO reading is ambiguous")
        assert "cannot be resolved" in r.excluded_fields["invoiceInfo.issueDateISO"] or \
               "forbids" in r.excluded_fields["invoiceInfo.issueDateISO"]


def test_a_gt_value_that_violates_the_schema_contract_is_a_gt_defect(records):
    """One invoiceDate is '8/4/2020 12:50:24 PM'. issueDate's own description forbids a time,
    so that is the annotation being wrong, not the model. Before the meridiem was handled this
    value produced no ISO and no reason — a silent gap, which is worse than either."""
    hit = [r for r in records.values()
           if "forbids" in (r.excluded_fields.get("invoiceInfo.issueDateISO") or "")]
    assert len(hit) == 1


def test_two_digit_years_pivot_on_the_corpus_not_on_posix():
    """DocILE spans the 1990s to the 2020s. POSIX's 1969 pivot would read '06-SEP-01' as 2001
    (right) but '02-29-20' as 2020 (right) and '99' as 2099 (wrong)."""
    assert P.date_to_iso("06-SEP-01") == "2001-09-06"
    assert P.date_to_iso("02-29-20") == "2020-02-29"
    assert P.date_to_iso("04-APR-1999") == "1999-04-04"
    assert P.date_to_iso("AUG12/02") == "2002-08-12"


def test_a_printed_zero_normalises_to_zero_and_not_to_null():
    """One totalDiscount is the string '.00'. Reading a printed zero as null would turn a
    stated fact into an absence."""
    assert P.money_amount(".00") == "0.00"
    assert P.money_amount("$5.00") == "5.00"
    assert P.money_amount("73,931.00") == "73931.00"
    assert P.money_amount(16.45) == "16.45"          # JSON numbers appear in this dataset
    assert P.money_amount(None) is None


# --------------------------------------------------------------------- record shape
def test_image_paths_are_relative_and_every_image_exists(records, adapter):
    for rec in records.values():
        assert not pathlib.Path(rec.image_path).is_absolute()
        assert (adapter.root / rec.image_path).exists(), rec.doc_id


def test_the_cluster_is_the_document(records):
    """93 distinct vendors over 100 documents, so nothing repeats enough to cluster on.
    Getting this wrong is what makes intervals too narrow."""
    assert len({r.cluster_id for r in records.values()}) == len(records)


def test_the_corpus_is_the_size_it_claims_to_be(records):
    assert len(records) == 100
    assert sum(len(r.gt.get("lineItems") or []) for r in records.values()) == 450
