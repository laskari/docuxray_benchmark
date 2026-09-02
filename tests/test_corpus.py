"""Whole-corpus invariants over the built ground truth.

Skips when gt/fatura.jsonl has not been built. These are the checks that catch a normaliser
regression silently changing thousands of values.
"""
import sys, pathlib, json, collections
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from core.canonical import read_jsonl
from core.normalize import normalise_date, normalise_money

ROOT = pathlib.Path(__file__).resolve().parent.parent
GT = ROOT / "gt" / "invoice" / "ground_truth.jsonl"
pytestmark = pytest.mark.skipif(not GT.exists(), reason="run scripts/build_gt.py first")


@pytest.fixture(scope="module")
def records():
    return read_jsonl(str(GT))


def test_every_document_is_present(records):
    assert len(records) == 10_000
    assert len({r.doc_id for r in records}) == 10_000


def test_fifty_templates_two_hundred_each(records):
    counts = collections.Counter(r.cluster_id for r in records)
    assert len(counts) == 50
    assert set(counts.values()) == {200}


def test_every_record_validates(records):
    for r in records:
        r.validate()


def test_no_gt_value_outside_annotated_fields(records):
    for r in records:
        assert set(r.gt) <= r.annotated_fields | {"lineItems"}


def test_annotated_and_excluded_never_overlap(records):
    for r in records:
        assert not (r.annotated_fields & set(r.excluded_fields))


def test_known_gt_errors_are_excluded(records):
    index = {r.doc_id: r for r in records}
    assert "invoiceInfo.issueDateISO" in index["Template38_Instance128"].excluded_fields
    assert "invoiceInfo.issueDateISO" in index["Template40_Instance58"].excluded_fields
    assert "totals.totalIncludingTax" in index["Template18_Instance4"].excluded_fields


def test_structural_exclusion_counts_match_the_register(records):
    """The counts in mapping/known_gt_errors.json must be what the build actually produced."""
    reasons = collections.Counter()
    for r in records:
        for reason in r.excluded_fields.values():
            if "BUYER and BILL_TO" in reason:
                reasons["collision"] += 1
            elif "multi-rate tax" in reason:
                reasons["multi_rate"] += 1

    assert reasons["collision"] == 200 * 4      # 200 docs x 4 customer paths
    assert reasons["multi_rate"] == 400 * 3     # 400 docs x 3 tax paths
    # SELLER==BUYER duplication is flagged, never excluded -- the page really prints it twice.
    dup = sum(1 for r in records if r.meta.get("seller_equals_buyer_address"))
    assert dup == 27, dup
    assert all(r.doc_id.endswith("Instance0") for r in records
               if r.meta.get("seller_equals_buyer_address"))


def test_no_label_prefix_leaked_into_any_gt_value(records):
    """Nothing like 'BALANCE_DUE : 481.84' should survive into ground truth."""
    bad = [
        (r.doc_id, p, v) for r in records for p, v in r.gt.items()
        if isinstance(v, str) and (" : " in v and v.split(" : ")[0].isupper())
    ]
    assert not bad, bad[:5]


def test_every_iso_date_is_iso_and_agrees_with_its_raw_twin(records):
    for r in records:
        for iso_path in ("invoiceInfo.issueDateISO", "invoiceInfo.dueDateISO"):
            iso = r.gt.get(iso_path)
            if not isinstance(iso, str):
                continue
            assert len(iso) == 10 and iso[4] == "-" and iso[7] == "-", (r.doc_id, iso)
            raw = r.gt.get(iso_path.replace("ISO", ""))
            if isinstance(raw, str):
                assert normalise_date(raw, formats=("%d-%b-%Y",)) == iso, (r.doc_id, raw, iso)


NUMERIC_PATHS = {
    "totals.totalIncludingTax", "totals.subtotal", "totals.taxAmount",
    "totals.taxPercentage", "totals.discountTotal", "totals.discountPercentage",
}


def test_every_numeric_is_a_plain_two_dp_string(records):
    for r in records:
        for p, v in r.gt.items():
            if p not in NUMERIC_PATHS or not isinstance(v, str):
                continue
            assert normalise_money(v) is not None, (r.doc_id, p, v)
            assert v == f"{normalise_money(v):.2f}", (r.doc_id, p, v)


def test_discount_is_never_negative(records):
    for r in records:
        v = r.gt.get("totals.discountTotal")
        if isinstance(v, str):
            assert not v.startswith("-"), (r.doc_id, v)


def test_currency_is_iso_or_absent_never_a_symbol(records):
    for r in records:
        c = r.gt.get("currency")
        if isinstance(c, str):
            assert c in {"EUR", "USD", "GBP", "INR"}, (r.doc_id, c)


def test_no_document_exposes_line_items_as_scoreable(records):
    for r in records:
        assert "lineItems" not in r.gt, r.doc_id
        assert not any(p.startswith("lineItems") for p in r.annotated_fields), r.doc_id


def test_line_items_are_still_captured_as_research_data(records):
    captured = [r for r in records if r.meta.get("gt_line_items")]
    assert len(captured) == 3363, len(captured)
    assert all(r.meta["line_items_status"] == "ok" for r in captured)


def test_absence_is_only_claimed_within_the_template_vocabulary(records):
    """A path may be ABSENT only if some instance of the same template annotates it."""
    annotated_by_template = collections.defaultdict(set)
    for r in records:
        annotated_by_template[r.cluster_id] |= r.present_fields
    for r in records:
        stray = r.absent_fields - annotated_by_template[r.cluster_id]
        # A path can be absent everywhere in a template only if the label exists in the
        # vocabulary but every instance's value is null -- legitimate, and rare.
        for p in stray:
            assert p.startswith("parties."), (r.doc_id, p)


def test_image_paths_are_relative_and_portable(records):
    """Ground truth is built on one machine and run on another. An absolute path here breaks
    every run elsewhere -- which it did, on 2026-09-01, until this test existed."""
    import pathlib
    for r in records[:200]:
        p = pathlib.Path(r.image_path)
        assert not p.is_absolute(), f"{r.doc_id}: {r.image_path}"
        assert p.parts[0] == "images", f"{r.doc_id}: {r.image_path}"
        assert "/sessions/" not in r.image_path and "/Users/" not in r.image_path
