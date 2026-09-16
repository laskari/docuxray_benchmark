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

    contained_leaves: FrozenSet[str] = frozenset()
    """Leaves that are scored based on substring containment rather than exact/ANLS match."""

    list_paths: FrozenSet[str] = frozenset()
    """Repeated sections needing row alignment before per-cell scoring. Score these only with
    a row-alignment matcher; a naive index-wise comparison is meaningless."""

    rate_keyed_lists: FrozenSet[str] = frozenset()
    """Repeated sections whose rows are paired on the RATE carried in each row's key, matched
    exactly. A tax line's identity IS its rate: it is printed, it is unique within every one of
    the 400 multi-rate documents, and it is the only thing that distinguishes five otherwise
    identical GST lines. Pairing anything else -- including a text-similarity score over labels
    that differ only in their digits -- risks answering the 1% line with the 18% entry.

    Empty by default, so every other section keeps the alignment it was measured under. Where
    it is set, a row carrying no rate is not force-paired: it falls through to the text matcher
    on what is left, so a real charge can never be paired with a tax line. Measured on this
    run, that fallback fires on 0 of 708 matched rows -- FATURA annotates no non-tax charge."""

    convention_criterion: bool = False
    """Publish the CONVENTION-ADJUSTED criterion beside the exact one (map contract 1.11).

    Off by default, so a doc type that has not enumerated and measured its notation
    differences reports one number, as before. The comparison always computes both verdicts;
    this decides whether the second is aggregated and reported. CORD and DOCILE leave it off
    and their results are byte-identical."""

    charge_label_paths: FrozenSet[str] = frozenset()
    """Row leaves holding a charge LABEL -- a charge name and a printed rate in one string --
    compared as those two facts rather than as one string (map contract 1.10, see
    MatchRule.CHARGE_LABEL). Declared as whole leaf paths, not leaf names, because 'key' is
    also CORD's and DOCILE's line-item caption leaf and those keep the text rule."""

    list_union_sources: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    """Scalar paths that may hold a row belonging to a repeated section, appended to the
    prediction side before alignment. Needed where the pipeline routes one printed fact to
    either place depending on how many of them there are: on FATURA's Template29 the GST line
    goes to totals.otherCharges and the VAT line to the scalar tax triple on 24 of 40 sampled
    documents. Scored against the list alone that template recovers 66.25%; against the union,
    96.25%. Same reasoning as merged_targets, one level up."""

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
    rate_keyed_lists=frozenset({"totals.otherCharges"}),
    charge_label_paths=frozenset({"totals.otherCharges[].key"}),
    convention_criterion=True,
    list_union_sources={
        # Multi-rate tax: Totals holds ONE tax, so a page printing several strands the rest.
        # The pipeline splits them across otherCharges and the scalar triple; the union puts
        # both back on the table before alignment. Map contract 1.9.
        "totals.otherCharges": ("totals.taxName", "totals.taxPercentage", "totals.taxAmount"),
    },
    merged_targets={
        # DocuXray routes note text BY CONTENT: a payment sentence goes to
        # paymentTerms.raw_text, everything else to customerMemo, and a mixed NOTE is split
        # across both. Measured on 10 documents; neither field alone is the right target.
        # "invoiceInfo.noteText": ("invoiceInfo.paymentTerms.raw_text",
        #                          "invoiceInfo.customerMemo"),
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
    contained_leaves=frozenset({"name"}),
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
