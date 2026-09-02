"""Document-type registry.

Everything the generic harness needs to know that is SPECIFIC to a document type lives here
and nowhere else. Adding a new type (receipt, bank statement) means adding one spec below --
not editing core/.

The rule for what belongs here: if the answer differs between an invoice and a receipt, it is
a DocTypeSpec field. If it is the same for both, it belongs in core/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Mapping, Tuple


@dataclass(frozen=True)
class DocTypeSpec:
    # --- identity -------------------------------------------------------------------------
    name: str
    """The value the production pipeline uses: GeminiExtractionService(doc_type=...),
    DocumentJudge.verify_sectional(document_type=...), get_postprocessor(...)."""

    schema_model: str
    """Pydantic model in ai/extraction/new_schema.py that defines this type's fields."""

    wrapper_key: str
    """Where the pipeline nests this type's data inside the extraction result."""

    # --- how the generic scorer should treat this type's fields ---------------------------
    merged_targets: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    """Derived comparison targets: one ground-truth value compared against the UNION of
    several prediction paths. Needed when the pipeline routes one printed fact to different
    fields depending on its content. The component paths are consumed, not scored separately."""

    collapse_address_objects: bool = True
    """Score parties.<role>.addressStructured as ONE merged comparison rather than five
    component leaves, so a correctly split address is neither rewarded nor punished."""

    identifier_leaves: FrozenSet[str] = frozenset()
    """Leaf names matched strictly (a near miss is a full miss), not fuzzily."""

    date_leaves: FrozenSet[str] = frozenset()
    """Leaf names holding a printed date. The matching '...ISO' twin is detected by suffix."""

    excluded_leaves: FrozenSet[str] = frozenset()
    """Never scoreable, with a published reason: reasoning text, self-reported confidence,
    run metadata. Any addition needs a written justification in the field map."""

    list_paths: FrozenSet[str] = frozenset()
    """Repeated sections needing row alignment before per-cell scoring. Score these only with
    a row-alignment matcher; a naive index-wise comparison is meaningless."""

    def __post_init__(self):
        if not self.name or not self.schema_model:
            raise ValueError("a DocTypeSpec needs a name and a schema_model")


# ------------------------------------------------------------------------------------------

_COMMON_EXCLUDED = frozenset({
    "categoryReasoning", "isOverflowPageReasoning",     # free-text rationale, no ground truth
    "documentTypeConfidence", "confidence",             # model self-report, not a document fact
    "extractionDate", "extractionDateISO", "pageCount", # run metadata
    "rawJsonString",                                    # unstructured escape hatch
})

_COMMON_IDENTIFIERS = frozenset({
    "documentNumber", "purchaseOrderNumber", "customerNumber", "trackingNumber",
    "itemCode", "accountNumber", "routingNumber", "referenceNumber",
})


INVOICE = DocTypeSpec(
    name="invoice",
    schema_model="InvoiceData",
    wrapper_key="invoiceOutputData",
    merged_targets={
        # DocuXray routes note text BY CONTENT: a payment sentence goes to
        # paymentTerms.raw_text, everything else to customerMemo, and a mixed NOTE is split
        # across both. Measured on 10 documents; neither field alone is the right target.
        "invoiceInfo.noteText": ("invoiceInfo.paymentTerms.raw_text",
                                 "invoiceInfo.customerMemo"),
    },
    identifier_leaves=_COMMON_IDENTIFIERS,
    date_leaves=frozenset({"issueDate", "dueDate", "serviceDate", "deliveryDate"}),
    excluded_leaves=_COMMON_EXCLUDED,
    list_paths=frozenset({"lineItems", "totals.otherCharges"}),
)


RECEIPT = DocTypeSpec(
    name="receipt",
    schema_model="ReceiptData",
    wrapper_key="receiptOutputData",
    # No merged targets known yet. Derive them the same way invoice's were: run a small sample,
    # check where the pipeline actually puts each ground-truth fact, and add a target only when
    # ONE fact provably lands across SEVERAL fields. See docs/01_KEY_MAPPING.md.
    merged_targets={},
    identifier_leaves=_COMMON_IDENTIFIERS,
    # Receipts use txnDate where invoices use issueDate.
    date_leaves=frozenset({"txnDate", "serviceDate"}),
    excluded_leaves=_COMMON_EXCLUDED,
    # ReceiptTotals.subtotal, .taxes and .otherCharges are LISTS on receipts where the invoice
    # equivalents are scalars. That is the single biggest structural difference between the two
    # types and the thing most likely to break a naive port.
    list_paths=frozenset({"lineItems", "totals.subtotal", "totals.taxes",
                          "totals.otherCharges"}),
)


REGISTRY: Dict[str, DocTypeSpec] = {s.name: s for s in (INVOICE, RECEIPT)}


def get(name: str) -> DocTypeSpec:
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown doc_type {name!r}; known: {sorted(REGISTRY)}. "
                       f"Add a DocTypeSpec in doctypes/__init__.py.") from None
