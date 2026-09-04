"""DocILE100 -> BenchmarkRecord.

Reads annotations_normalized/ (annotations/ is the untouched source; five records arrived
wrapped in a redundant {"Invoice": {...}} envelope and normalize_annotations.py unwrapped them).

Two things make this adapter structurally different from the FATURA one, both of them
consequences of the dataset rather than choices:

1. NO AUTHORITATIVE ABSENCE. FATURA licenses absence from template vocabulary — 200 instances
   share one layout, so a label missing from one instance but present in its siblings is
   genuinely absent from that page. DocILE100 is 100 unrelated documents, 93 distinct vendors,
   no repeating structure. A null is indistinguishable between "not printed" and "not
   recorded", so every null here is state (iii): written to NEITHER gt nor annotated_fields,
   and therefore outside every denominator. This dataset publishes no hallucination metric.
   See absence_policy in the map.

2. LINE ITEMS ARE SCORED. The opposite of the FATURA decision, for a stated reason: FATURA
   annotates zero line-item content, whereas DocILE100 annotates 450 rows directly and 69 of
   100 documents have those rows summing exactly to a stated subtotal or total. Rows go into
   gt["lineItems"] and are aligned by core/rows.py, never compared index-wise.

Provenance caveat: the Hugging Face card for Humayoun/DocILE100 is empty — no methodology, no
licence, no attribution. Treat as a development set; publish only the adjudicated subset.
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import BenchmarkRecord            # noqa: E402
from datasets import docile_parsers as P              # noqa: E402
from datasets.base import DatasetAdapter              # noqa: E402

SECTIONS = ("Vendor Information", "Itemized Purchases", "Financial Summary")

# --------------------------------------------------------------------------- the rule table
# The executable half of mapping/docile_invoice.map.yaml. check_contract asserts the two agree
# on every label's mapped/unmapped status, so a map edited without a code change stops the
# build instead of silently changing results.

Target = Tuple[str, str]                              # (schema path, parser name)

SCALAR_RULES: Dict[str, Tuple[Target, ...]] = {
    "Vendor Information.vendorName":      (("parties.seller.name", "passthrough_strip"),),
    "Vendor Information.invoiceNumber":   (("invoiceInfo.documentNumber", "passthrough_strip"),),
    "Vendor Information.invoiceDate":     (("invoiceInfo.issueDate", "passthrough_strip"),
                                           ("invoiceInfo.issueDateISO", "date_to_iso")),
    "Financial Summary.invoiceSubTotal":  (("totals.subtotal", "money_amount"),),
    "Financial Summary.taxes":            (("totals.taxAmount", "money_amount"),),
    "Financial Summary.totalDiscount":    (("totals.discountTotal", "money_amount"),),
    "Financial Summary.invoiceTotal":     (("totals.totalIncludingTax", "money_amount"),),
}

# Both land in the same schema list, which is what makes this a set-recovery measurement
# rather than a scalar comparison. The printed label is kept as the charge key.
CHARGE_LABELS: Dict[str, str] = {
    "Financial Summary.deliveryShippingCharges": "Shipping",
    "Financial Summary.otherCharges": "Other",
}

# GT row key -> the schema leaves a prediction may legitimately use for it. A tuple with more
# than one entry is a UNION: the GT value is compared against whichever leaf the model filled.
LINE_ITEM_TARGETS: Dict[str, Tuple[str, ...]] = {
    "description":    ("description",),
    "itemCode":       ("itemCode",),
    "quantity":       ("quantity",),
    "unitPrice":      ("unitPrice",),
    # totalItemPrice has two valid destinations: lineTotalExcludingTax's description says to
    # use it when no tax applies on the line (true for every DocILE100 row), but the
    # instruction is conditional and a model reading the row as tax-inclusive has not got the
    # NUMBER wrong. Union, so the arithmetic is scored and the routing is not.
    "lineTotal":      ("lineTotalExcludingTax", "lineTotalIncludingTax"),
    "serviceDate":    ("serviceDate",),
    "serviceDateISO": ("serviceDateISO",),
}

# totals.otherCharges is the doc type's OTHER repeated section. It needs its own targets and
# its own alignment key: a charge is identified by its printed label, not by a description, and
# pooling its rows with line items would report one meaningless average over two different
# things. This is what the map calls charge_recovery.
CHARGE_TARGETS: Dict[str, Tuple[str, ...]] = {
    "key":   ("key",),
    "value": ("value",),
}

#: list path -> (row targets, alignment keys). Read by core/metrics.score_document_lists.
ROW_TARGETS: Dict[str, Dict[str, Tuple[str, ...]]] = {}      # filled below
ROW_MATCH_KEYS: Dict[str, Tuple[str, ...]] = {}              # filled below

# The row keys used to ALIGN rows, in preference order. Alignment accepts either GT column
# because the annotation's choice drifts between documents: itemName holds the real
# description on most files but is the generic label "60 Spot" on the 44-row radio-log family,
# where the description sits in itemCode instead.
LINE_ITEM_MATCH_KEYS: Tuple[str, ...] = ("description", "descriptionAlt")

ROW_TARGETS = {"lineItems": LINE_ITEM_TARGETS, "totals.otherCharges": CHARGE_TARGETS}
ROW_MATCH_KEYS = {"lineItems": LINE_ITEM_MATCH_KEYS, "totals.otherCharges": ("key",)}

#: Which PREDICTION leaves carry each section's identifying text. A line item is identified by
#: its description (or the code, when a model routes it there); a document-level charge is
#: identified by its printed label. Without this, charges were aligned against a `description`
#: leaf a Charge object does not have, and a perfect prediction scored 0 of 6 recovered.
ROW_TEXT_LEAVES: Dict[str, Tuple[str, ...]] = {
    "lineItems": ("description", "itemCode"),
    "totals.otherCharges": ("key",),
}

ROW_RULES: Dict[str, Tuple[str, str]] = {          # GT row key -> (source label, parser)
    "description":    ("itemName", "passthrough_strip"),
    "descriptionAlt": ("itemCode", "passthrough_strip"),
    "itemCode":       ("itemCode", "passthrough_strip"),
    "quantity":       ("itemeQty", "money_amount"),
    "unitPrice":      ("itemPrice", "money_amount"),
    "lineTotal":      ("totalItemPrice", "money_amount"),
    "serviceDate":    ("date", "passthrough_strip"),
    "serviceDateISO": ("date", "date_to_iso"),
}

UNMAPPED_LABELS = frozenset({
    "Itemized Purchases.itemCaseSize",     # broadcast "Len" seconds; no schema home
    "Itemized Purchases.time",             # air time; LineItem has no service time
    "Financial Summary.credit",            # no schema field means "credit applied"
})

NOT_ANNOTATED_LABELS = frozenset({
    "Itemized Purchases.itemWeight",       # a key on all 450 rows, null on all 450
})


def iter_labels(raw: Any) -> Iterable[Tuple[str, Any]]:
    """(label, value) at the granularity the field map works at: 'Section.key'.

    Line-item keys are yielded once per distinct key present across the rows, not once per
    row, so the keyset id describes the document's SCHEMA rather than its length.
    """
    for section, body in (raw or {}).items():
        if isinstance(body, dict):
            for k, v in body.items():
                yield f"{section}.{k}", v
        elif isinstance(body, list):
            seen: Set[str] = set()
            for row in body:
                if isinstance(row, dict):
                    for k in row:
                        if k not in seen:
                            seen.add(k)
                            yield f"{section}.{k}", None
        else:
            yield section, body


class DocileAdapter(DatasetAdapter):
    source_name = "docile100"

    #: exposed for core/rows.py — which schema leaves each GT row key may be scored against
    row_targets = ROW_TARGETS
    row_match_keys = ROW_MATCH_KEYS
    row_text_leaves = ROW_TEXT_LEAVES

    def iter_raw(self) -> Iterable[tuple]:
        src = self.root / "annotations_normalized"
        if not src.is_dir():
            raise FileNotFoundError(
                f"{src} not found. Run normalize_annotations.py in the dataset root first — "
                f"annotations/ holds five records wrapped in an {{'Invoice': ...}} envelope "
                f"and reading it directly would silently drop them.")
        for path in sorted(src.glob("*.json")):
            yield path.stem, json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ line items
    def _rows(self, raw: Any, excluded: Dict[str, str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for idx, src in enumerate(raw.get("Itemized Purchases") or []):
            if not isinstance(src, dict):
                continue
            row: Dict[str, Any] = {}
            for gt_key, (label, parser_name) in ROW_RULES.items():
                row[gt_key] = P.get(parser_name)(src.get(label), key=label)
            # A row asserting nothing at all cannot be aligned or scored. Recorded rather than
            # dropped silently, because a vanishing row would quietly shrink row recall's
            # denominator and flatter the model.
            if not any(v is not None for v in row.values()):
                excluded[f"lineItems[{idx}]"] = "row asserts no value in any mapped column"
                continue
            date_flag = P.classify_date(src.get("date"))
            if date_flag is not None:
                row["serviceDateISO"] = None
                excluded[f"lineItems[{idx}].serviceDateISO"] = _date_reason(date_flag)
            out.append(row)
        return out

    # ------------------------------------------------------------------ record
    def build(self, doc_id: str, raw: Any) -> BenchmarkRecord:
        gt: Dict[str, Any] = {}
        annotated: Set[str] = set()
        excluded: Dict[str, str] = {}

        # ---- scalars ------------------------------------------------------------
        for label, targets in SCALAR_RULES.items():
            section, _, key = label.partition(".")
            value = (raw.get(section) or {}).get(key)
            for path, parser_name in targets:
                parsed = P.get(parser_name)(value, key=label)
                if parsed is None:
                    continue
                gt[path] = parsed
                annotated.add(path)

        # The ISO twin is lost on its own terms, not because the raw value was missing, so the
        # reason is recorded per document and published rather than left as an unexplained gap.
        flag = P.classify_date((raw.get("Vendor Information") or {}).get("invoiceDate"))
        if flag is not None:
            excluded["invoiceInfo.issueDateISO"] = _date_reason(flag)

        # ---- charges ------------------------------------------------------------
        charges = []
        for label, charge_key in CHARGE_LABELS.items():
            section, _, key = label.partition(".")
            parsed = P.get("money_amount")((raw.get(section) or {}).get(key), key=label)
            if parsed is not None:
                charges.append({"key": charge_key, "value": parsed})
        if charges:
            gt["totals.otherCharges"] = charges
            annotated.add("totals.otherCharges")

        # ---- line items ---------------------------------------------------------
        rows = self._rows(raw, excluded)
        if rows:
            gt["lineItems"] = rows
            annotated.add("lineItems")
        else:
            # 3 of 100 documents have no itemised table — one verified by eye as a Vendor
            # Remittance Advice, which legitimately has none. Not annotated rather than
            # "annotated as zero rows": counting it as a row-recall denominator of 0 would
            # make the metric undefined, and counting it as a correct empty table would credit
            # the model for a table that was never asked for. Reported separately as a
            # table-presence question. See document_heterogeneity_policy in the map.
            excluded["lineItems"] = "document has no itemised table (0 GT rows)"

        rec = BenchmarkRecord(
            doc_id=doc_id,
            source_dataset=self.source_name,
            doc_type=self.spec.name,
            image_path=str(pathlib.PurePosixPath("images") / f"{doc_id}.png"),
            # 93 distinct vendors over 100 documents, so the document IS the repeating unit and
            # effective N is essentially 100 — unlike FATURA, where 10,000 documents cluster
            # into 50 templates. Intervals bootstrap over documents.
            cluster_id=doc_id,
            keyset_id=self.keyset_id(l for l, _ in iter_labels(raw)),
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            meta={
                "labels": sorted(l for l, _ in iter_labels(raw)),
                "n_gt_rows": len(rows),
                "vendor": (raw.get("Vendor Information") or {}).get("vendorName"),
                "row_targets": {k: {gk: list(v) for gk, v in tg.items()}
                                for k, tg in ROW_TARGETS.items()},
                "row_match_keys": {k: list(v) for k, v in ROW_MATCH_KEYS.items()},
                "row_text_leaves": {k: list(v) for k, v in ROW_TEXT_LEAVES.items()},
            },
        )
        rec.validate()
        return rec

    # ------------------------------------------------------------------ contract
    def check_contract(self, map_path: str) -> None:
        """Assert this module and the reviewed map agree on every label's status.

        Compares three sets in both directions. A map row added without a code change, or a
        rule added without a map row, fails here — which is the point: ground truth is not
        written from a file nobody reviewed.
        """
        import yaml

        doc = yaml.safe_load(pathlib.Path(map_path).read_text(encoding="utf-8"))

        def labels_of(section: str) -> Set[str]:
            out: Set[str] = set()
            for row in doc.get(section) or []:
                out.update(row.get("labels") or ([row["label"]] if "label" in row else []))
            return out

        map_mapped = labels_of("mappings")
        map_unmapped = labels_of("unmapped")

        code_mapped = set(SCALAR_RULES) | set(CHARGE_LABELS) | {
            f"Itemized Purchases.{label}"
            for label, _ in ROW_RULES.values()
        }
        problems = []

        missing_in_map = code_mapped - map_mapped
        if missing_in_map:
            problems.append(f"mapped in code, absent from the map's mappings: "
                            f"{sorted(missing_in_map)}")
        missing_in_code = map_mapped - code_mapped
        if missing_in_code:
            problems.append(f"in the map's mappings, not mapped by code: "
                            f"{sorted(missing_in_code)}")
        if map_unmapped != set(UNMAPPED_LABELS):
            problems.append(f"unmapped disagrees — map: {sorted(map_unmapped)}, "
                            f"code: {sorted(UNMAPPED_LABELS)}")
        overlap = map_mapped & (set(UNMAPPED_LABELS) | set(NOT_ANNOTATED_LABELS))
        if overlap:
            problems.append(f"labels both mapped and unmapped/not_annotated: {sorted(overlap)}")

        if problems:
            raise AssertionError(
                "docile_invoice.py and " + map_path + " have drifted:\n  - "
                + "\n  - ".join(problems)
                + "\n\nFix by amending ONE side with a CHANGELOG entry. Never by loosening "
                  "this check.")


def _date_reason(flag: str) -> str:
    if flag == P.AMBIGUOUS_DATE:
        return ("day and month are both <= 12 and differ, so M/D vs D/M cannot be resolved; "
                "DocILE states no locale and guessing would score the field on an assumption")
    if flag == P.GT_DEFECT_DATE:
        return ("GT value embeds a time component, which invoiceInfo.issueDate's own schema "
                "description forbids; a GT defect, not a model failure")
    return f"unresolvable date ({flag})"
