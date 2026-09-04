"""FATURA -> BenchmarkRecord.

The rule table below is the executable form of mapping/fatura_field_map.yaml. The YAML is the
REVIEWED CONTRACT; this module implements it. `check_against_yaml()` asserts the two agree on
which labels are mapped and unmapped, so the code cannot silently drift from what was signed
off. Any disagreement is a hard error, never a warning.

Party roles are resolved in code rather than in the YAML because the mapping is conditional
(BILL_TO stands in for BUYER only when BUYER is absent) -- see party_role_policy in the YAML.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import sys, pathlib
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import BenchmarkRecord                      # noqa: E402
from datasets.base import DatasetAdapter                        # noqa: E402
from datasets import fatura_parsers as P                           # noqa: E402
from core.normalize import ABSENT, AMBIGUOUS_CURRENCY           # noqa: E402

META_KEYS = {"Image_Name", "_line_items_status"}


@dataclass(frozen=True)
class Target:
    path: str
    parser: str
    kwargs: Tuple[Tuple[str, Any], ...] = ()

    def run(self, raw: Any, key: str) -> Optional[str]:
        return P.get(self.parser)(raw, key=key, **dict(self.kwargs))


def T(path: str, parser: str, **kwargs) -> Target:
    return Target(path, parser, tuple(sorted(kwargs.items())))


# ---------------------------------------------------------------------------------------
# Scalar labels
# ---------------------------------------------------------------------------------------

SCALAR_RULES: Dict[str, List[Target]] = {
    "NUMBER":     [T("invoiceInfo.documentNumber", "passthrough_strip")],
    "PO_NUMBER":  [T("invoiceInfo.purchaseOrderNumber", "passthrough_strip")],
    "DATE":       [T("invoiceInfo.issueDate", "passthrough_strip"),
                   T("invoiceInfo.issueDateISO", "date_to_iso")],
    "DUE_DATE":   [T("invoiceInfo.dueDate", "passthrough_strip"),
                   T("invoiceInfo.dueDateISO", "date_to_iso")],
    "TOTAL":      [T("totals.totalIncludingTax", "money_amount"),
                   T("currency", "money_currency")],
    # AMOUNT_DUE equals TOTAL in 399/399 co-occurrences -- the same fact restated, so it writes
    # the same path and is deduplicated rather than double-counted.
    "AMOUNT_DUE": [T("totals.totalIncludingTax", "money_amount"),
                   T("currency", "money_currency")],
    "SUB_TOTAL":  [T("totals.subtotal", "money_amount"),
                   T("currency", "money_currency")],
    "TAX":        [T("totals.taxName", "tax_name"),
                   T("totals.taxPercentage", "tax_pct"),
                   T("totals.taxAmount", "tax_amount"),
                   T("currency", "money_currency")],
    # GT discount is a POSITIVE MAGNITUDE: invoice_postprocessor._normalize_totals converts a
    # negative discountTotal to its absolute value, so a negative GT would make the
    # postprocessed arm score worse than bare extraction as a pure sign artifact.
    "DISCOUNT":   [T("totals.discountPercentage", "pct_in_parens"),
                   T("totals.discountTotal", "money_abs")],
    # Derived target: compared against paymentTerms.raw_text + customerMemo merged. DocuXray
    # routes note text by content and splits a mixed NOTE across both -- see line 'NOTE' in the
    # map's note_policy. Neither field alone is the right target.
    "NOTE":       [T("invoiceInfo.noteText", "passthrough_strip")],
}

GST_KEY_RE = re.compile(r"^GST\(\s*\d+(?:\.\d+)?\s*%\)$", re.IGNORECASE)
GST_RULES: List[Target] = [
    T("totals.taxName", "literal", value="GST"),
    T("totals.taxPercentage", "rate_from_key"),
    T("totals.taxAmount", "money_amount"),
]

# Labels with no defensible schema home. Each reason is published verbatim in the YAML.
UNMAPPED_LABELS = {
    "TITLE", "TOTAL_WORDS", "CONDITIONS", "LINE_ITEMS_TEXT",
    "GSTIN", "GSTIN_SELLER", "GSTIN_BUYER",         # no tax-ID field exists on Party
    "PAYMENT_DETAILS",                               # no bank/remittance group exists
}

# ---------------------------------------------------------------------------------------
# Party roles  (see party_role_policy in fatura_field_map.yaml)
# ---------------------------------------------------------------------------------------

PARTY_SUBFIELD: Dict[str, Tuple[str, str]] = {
    "Name":    ("name", "passthrough_strip"),
    # The OBJECT, not the .address leaf: the matcher merges all five components before
    # comparing. See address_policy in the map and MatchRule.ADDRESS.
    "Address": ("addressStructured", "address_whole"),
    "Tel":     ("phone", "phone_normalise"),
    "Email":   ("email", "passthrough_strip"),
    # 'Site' is deliberately absent: Party has no website field. SCHEMA GAP.
}
PARTY_UNMAPPED_SUBFIELDS = {"Site"}
PARTY_LABELS = ("SELLER", "BUYER", "BILL_TO", "SEND_TO")

# Which sub-keys each party block can carry in the source. SELLER has no Tel: the FATURA label
# vocabulary has no SELLER_TEL, so parties.seller.phone is not_annotated rather than unmapped.
PARTY_LABEL_SUBFIELDS: Dict[str, Tuple[str, ...]] = {
    "SELLER":  ("Name", "Address", "Email", "Site"),
    "BUYER":   ("Name", "Address", "Tel", "Email", "Site"),
    "BILL_TO": ("Name", "Address", "Tel", "Email", "Site"),
    "SEND_TO": ("Name", "Address", "Tel", "Email", "Site"),
}


def resolve_roles(labels: Set[str]) -> Tuple[Dict[str, str], Optional[str]]:
    """Map FATURA party labels to schema roles.

    Returns (label -> schema role, collision_reason). BILL_TO stands in for BUYER only when
    BUYER is absent; when both are present the schema's single `customer` slot cannot hold two
    distinct parties, so customer is excluded for that document (200 files).
    """
    roles: Dict[str, str] = {}
    collision = None
    if "SELLER" in labels:
        roles["SELLER"] = "seller"
    if "SEND_TO" in labels:
        roles["SEND_TO"] = "shipTo"
    if "BUYER" in labels and "BILL_TO" in labels:
        collision = "BUYER and BILL_TO both present; schema has one customer slot"
    elif "BUYER" in labels:
        roles["BUYER"] = "customer"
    elif "BILL_TO" in labels:
        roles["BILL_TO"] = "customer"
    return roles, collision


# ---------------------------------------------------------------------------------------
# Known GT defects  (mapping/known_gt_errors.json)
# ---------------------------------------------------------------------------------------

DOC_EXCLUSIONS: Dict[str, Dict[str, str]] = {
    "Template38_Instance128": {
        "invoiceInfo.issueDate": "GT DATE holds a due date",
        "invoiceInfo.issueDateISO": "GT DATE holds a due date",
    },
    "Template40_Instance58": {
        "invoiceInfo.issueDate": "GT DATE holds a due date",
        "invoiceInfo.issueDateISO": "GT DATE holds a due date",
    },
    "Template18_Instance4": {
        "totals.totalIncludingTax": "GT TOTAL holds 'DUE_AMOUNT : 240.38 $'",
    },
}

TAX_PATHS = ("totals.taxName", "totals.taxPercentage", "totals.taxAmount")


# ---------------------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------------------

def iter_labels(raw: dict):
    """(label, value) pairs for step 1's label inventory. Nested party and payment blocks are
    flattened to leaves, because that is the granularity the map works at."""
    for k, v in raw.items():
        if k in META_KEYS:
            continue
        if isinstance(v, dict):
            for sk, sv in v.items():
                yield f"{k}.{sk}", sv
        else:
            yield k, v


class FaturaAdapter(DatasetAdapter):
    source_name = "FATURA"

    def __init__(self, dataset_root: str, spec=None, *, doc_type: str = "invoice"):
        import doctypes as _dt
        self.spec = spec or _dt.get(doc_type)
        self.root = pathlib.Path(dataset_root).expanduser()
        self.ann_dir = self.root / "modified_annotations"
        self.img_dir = self.root / "images"
        self.doc_type = self.spec.name
        if not self.ann_dir.is_dir():
            raise FileNotFoundError(f"no modified_annotations under {self.root}")
        self._template_vocab: Dict[str, Set[str]] = {}
        self.stats: Dict[str, int] = {}

    # -------------------------------------------------------------- template vocabulary

    def build_template_vocab(self) -> Dict[str, Set[str]]:
        """Union of labels seen across every instance of a template.

        This is what makes absence AUTHORITATIVE: if a template renders DUE_DATE on some
        instances, its absence on another instance means the field is genuinely not on that
        page, so a model emission there is a hallucination candidate. Labels never seen for a
        template are state (iii) -- excluded from every denominator.

        ASSUMPTION requiring verification before the pilot: eyeball 5 documents per label where
        the label is absent and confirm the field really is absent from the rendered page.
        """
        vocab: Dict[str, Set[str]] = {}
        for name, data in self._iter_raw():
            tmpl = name.split("_", 1)[0]
            vocab.setdefault(tmpl, set()).update(self._labels(data))
        self._template_vocab = vocab
        return vocab

    def iter_raw(self):
        return self._iter_raw()

    def check_contract(self, map_path: str) -> None:
        """Assert this module still agrees with the reviewed map, and that every parser it
        names is registered. Gates ground-truth builds -- see docs/01_KEY_MAPPING.md."""
        check_against_yaml(map_path)
        check_parsers_exist()

    def _iter_raw(self) -> Iterable[Tuple[str, dict]]:
        for path in sorted(self.ann_dir.glob("*.json")):
            with open(path, encoding="utf-8") as fh:
                yield path.stem, json.load(fh)

    @staticmethod
    def _labels(data: dict) -> Set[str]:
        return {k for k in data if k not in META_KEYS}

    # -------------------------------------------------------------- per-label expansion

    @staticmethod
    def _targets_for(label: str) -> List[Target]:
        if label in SCALAR_RULES:
            return SCALAR_RULES[label]
        if GST_KEY_RE.match(label):
            return GST_RULES
        return []

    def _party_targets(self, label: str, role: str, data: dict) -> List[Tuple[Target, Any, str]]:
        block = data.get(label) or {}
        out = []
        if not isinstance(block, dict):
            return out
        # Only the sub-keys this label can carry. SELLER has no Tel, so parties.seller.phone is
        # never generated -- it is not_annotated, not an unparseable miss.
        for sub in PARTY_LABEL_SUBFIELDS.get(label, ()):
            if sub not in PARTY_SUBFIELD:
                continue
            leaf, parser_name = PARTY_SUBFIELD[sub]
            out.append((T(f"parties.{role}.{leaf}", parser_name), block.get(sub), f"{label}.{sub}"))
        return out

    def _party_vocab_paths(self, roles: Dict[str, str]) -> Set[str]:
        return {
            f"parties.{role}.{PARTY_SUBFIELD[sub][0]}"
            for label, role in roles.items()
            for sub in PARTY_LABEL_SUBFIELDS.get(label, ())
            if sub in PARTY_SUBFIELD
        }

    # -------------------------------------------------------------- record construction

    def build(self, name: str, data: dict) -> BenchmarkRecord:
        labels = self._labels(data)
        tmpl = name.split("_", 1)[0]
        vocab = self._template_vocab.get(tmpl, labels)

        roles, collision = resolve_roles(labels)
        vocab_roles, _ = resolve_roles(vocab)

        excluded: Dict[str, str] = {}
        notes: Dict[str, Any] = {}

        # ---- structural exclusions -------------------------------------------------
        gst_labels = [l for l in labels if GST_KEY_RE.match(l)]

        # Multi-rate tax: capture the individual printed tax lines as their own GT structure.
        # The three SCALAR paths stay excluded (Totals cannot hold more than one), but the facts
        # are still on the page, so they are recorded here and scored against totals.otherCharges
        # -- whose own description names "Tax" as an example of a document-level modifier. That
        # turns an unrepresentable field into a measurable recovery rate instead of a blind spot.
        if len(gst_labels) > 1 or (gst_labels and "TAX" in labels):
            lines = []
            for lbl in sorted(gst_labels):
                lines.append({
                    "key": "GST",
                    "percentage": P.get("rate_from_key")(data.get(lbl), key=lbl),
                    "amount": P.get("money_amount")(data.get(lbl), key=lbl),
                })
            if "TAX" in labels:
                raw = data.get("TAX")
                lines.append({
                    "key": P.get("tax_name")(raw, key="TAX"),
                    "percentage": P.get("tax_pct")(raw, key="TAX"),
                    "amount": P.get("tax_amount")(raw, key="TAX"),
                })
            notes["multi_tax_template"] = True
            notes["tax_lines"] = lines

        # No tax label at all -> totalIncludingTax and totalExcludingTax are the same figure on
        # this page, so the distinction cannot be got wrong. Tagged, not excluded: every
        # total-field result is reported over all docs AND over the taxed subset, and the gap
        # between the two is published. See no_tax_slice_policy in the map.
        if not gst_labels and "TAX" not in labels:
            notes["no_tax_label"] = True
        if len(gst_labels) > 1 or (gst_labels and "TAX" in labels):
            reason = ("multi-rate tax: InvoiceData.Totals is scalar and cannot hold "
                      f"{len(gst_labels) + (1 if 'TAX' in labels else 0)} tax lines (SCHEMA GAP)")
            for p in TAX_PATHS:
                excluded[p] = reason
            notes["multi_rate_tax"] = sorted(gst_labels) + (["TAX"] if "TAX" in labels else [])

        if collision:
            for leaf, _ in PARTY_SUBFIELD.values():
                excluded[f"parties.customer.{leaf}"] = collision

        # NOT excluded: SELLER.Address == BUYER.Address (27 docs) and SELLER.Email ==
        # BUYER.Email (16). Verified against images/Template1_Instance0.jpg -- the page really
        # does print the same address and email in both the seller block and the Bill-to block.
        # Ground truth is right and so is a model that reproduces it. Flagged in meta so the
        # duplication can be sliced out of a report, never dropped from the denominator.
        seller, buyer = data.get("SELLER") or {}, data.get("BUYER") or {}
        if isinstance(seller, dict) and isinstance(buyer, dict):
            if seller.get("Address") and seller.get("Address") == buyer.get("Address"):
                notes["seller_equals_buyer_address"] = True
            if seller.get("Email") and seller.get("Email") == buyer.get("Email"):
                notes["seller_equals_buyer_email"] = True

        excluded.update(DOC_EXCLUSIONS.get(name, {}))

        # ---- collect writes from labels present in THIS file ------------------------
        # Each write carries three things, because "the source said nothing here" and "the
        # source said something we could not parse" are different facts with different
        # consequences: the first is a scoreable absence, the second is an exclusion.
        writes: Dict[str, List[Tuple[str, bool, Optional[str]]]] = {}

        def record(path: str, source: str, raw: Any, value: Optional[str]) -> None:
            raw_empty = raw is None or (isinstance(raw, str) and not raw.strip())
            writes.setdefault(path, []).append((source, raw_empty, value))

        for label in sorted(labels):
            if label in UNMAPPED_LABELS or label in PARTY_LABELS or label == "LINE_ITEMS":
                continue
            raw = data.get(label)
            for target in self._targets_for(label):
                record(target.path, label, raw, target.run(raw, label))

        for label, role in roles.items():
            for target, raw, source in self._party_targets(label, role, data):
                record(target.path, source, raw, target.run(raw, source))

        # ---- vocabulary paths: everything this TEMPLATE can annotate ----------------
        vocab_paths: Set[str] = set()
        for label in vocab:
            if label in UNMAPPED_LABELS or label in PARTY_LABELS or label == "LINE_ITEMS":
                continue
            vocab_paths.update(t.path for t in self._targets_for(label))
        vocab_paths |= self._party_vocab_paths(vocab_roles)

        # ---- resolve to ground truth ------------------------------------------------
        gt: Dict[str, Any] = {}
        annotated: Set[str] = set()

        for path in sorted(vocab_paths):
            if path in excluded:
                continue
            candidates = writes.get(path, [])
            values = [v for _, _, v in candidates if v is not None]
            unparseable = [s for s, raw_empty, v in candidates if not raw_empty and v is None]

            if path == "currency":
                unambiguous = [v for v in values if v != AMBIGUOUS_CURRENCY]
                if unambiguous:
                    values = unambiguous
                elif values:
                    excluded[path] = "only a bare '$' as currency evidence; ambiguous, not guessed"
                    continue

            if values:
                if len(set(values)) > 1:
                    excluded[path] = (
                        "conflicting GT labels: "
                        + ", ".join(f"{s}={v}" for s, _, v in candidates if v is not None)
                    )
                    continue
                gt[path] = values[0]                    # state (i): annotated and present
            elif unparseable:
                # The source printed something we could not parse. Excluding is the honest
                # move: scoring it as absent would credit the model for emitting nothing.
                excluded[path] = f"GT value present but unparseable ({', '.join(sorted(set(unparseable)))})"
                continue
            else:
                # Either no label wrote here, or every writer's raw value was null/blank. The
                # template's vocabulary covers this path, so absence is authoritative.
                gt[path] = ABSENT                       # state (ii): scoreable absence
            annotated.add(path)

        # ---- line items -------------------------------------------------------------
        li_status = data.get("_line_items_status")
        if li_status == "ok" and isinstance(data.get("LINE_ITEMS"), list):
            rows = []
            for row in data["LINE_ITEMS"]:
                if not isinstance(row, dict):
                    continue
                rows.append({
                    "description": P.get("passthrough_strip")(row.get("description")),
                    "quantity": P.get("money_amount")(row.get("quantity")),
                    "unitPrice": P.get("money_amount")(row.get("price")),
                })
            if rows:
                # NOT added to annotated_fields -- line items are RESEARCH DATA on this dataset,
                # never scored. See line_items_policy in the map. Measured 2026-09-01 over 16
                # documents with real DocuXray output:
                #   * GT holds 18 rows where the model extracted 63 -- 29% recovery
                #   * 9 of 16 documents have ZERO GT rows while rows are plainly printed
                #   * `_line_items_status == "ok"` is NOT a safeguard: Template4_Instance0 is
                #     'ok' with 1 GT row against 4 printed, verified against the page image
                #   * two documents verified pixel-by-pixel (Template1, Template4) -- the model
                #     is right and the ground truth is wrong in both
                # Scoring against this would measure agreement with a weaker extractor, on a
                # subset the status flag cannot even identify reliably.
                notes["gt_line_items"] = rows
                notes["gt_line_item_rows"] = len(rows)

        record_obj = BenchmarkRecord(
            doc_id=name,
            source_dataset="FATURA",
            doc_type=self.doc_type,
            # RELATIVE to the dataset root, never absolute. Ground truth is built on one
            # machine and run on another (the sandbox mounts these folders at a different
            # path), so an absolute path here silently breaks every run elsewhere. The runner
            # resolves it against config paths.dataset.
            image_path=str(pathlib.PurePosixPath("images")
                           / (data.get("Image_Name") or f"{name}.jpg")),
            cluster_id=tmpl,
            keyset_id=keyset_id(labels),
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            meta={
                "labels": sorted(labels),
                "template_vocab_size": len(vocab),
                "line_items_status": li_status,
                "roles": roles,
                **notes,
            },
        )
        record_obj.validate()
        return record_obj

    def iter_records(self) -> Iterable[BenchmarkRecord]:
        if not self._template_vocab:
            self.build_template_vocab()
        for name, data in self._iter_raw():
            yield self.build(name, data)


def keyset_id(labels: Iterable[str]) -> str:
    payload = "|".join(sorted(labels)).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:10]


# ---------------------------------------------------------------------------------------
# Contract check -- the code may not drift from the reviewed YAML
# ---------------------------------------------------------------------------------------

def _code_label_status() -> Dict[str, str]:
    """Every FATURA label token the code knows, and whether it maps to a schema path."""
    status: Dict[str, str] = {}
    for label in SCALAR_RULES:
        status[label] = "mapped"
    # Captured as research data, never scored -- see line_items_policy in the map.
    status["LINE_ITEMS"] = "research_only"
    for label in UNMAPPED_LABELS:
        status[label] = "unmapped"
    for label, subs in PARTY_LABEL_SUBFIELDS.items():
        for sub in subs:
            status[f"{label}.{sub}"] = "mapped" if sub in PARTY_SUBFIELD else "unmapped"
    for key in META_KEYS:
        status[key] = "unmapped"
    return status


def check_against_yaml(yaml_path: str) -> None:
    """Assert the executable rule table agrees with the signed-off map.

    Compares label -> mapped/unmapped on both sides using the YAML's machine-readable `labels:`
    lists. Any disagreement is a hard error: resolve it by amending the YAML (with a CHANGELOG
    entry) or the code, never by loosening this check. Silent drift between the reviewed
    contract and the thing that actually runs is how a benchmark stops meaning anything.
    """
    import yaml as _yaml

    doc = _yaml.safe_load(open(yaml_path, encoding="utf-8"))
    yaml_status: Dict[str, str] = {}
    regex_rows = 0
    for row in doc["mappings"]:
        if "labels" not in row:
            raise AssertionError(
                f"map row {row.get('label')!r} has no machine-readable `labels:` list; "
                "the contract check cannot verify it."
            )
        if row.get("label_regex"):
            regex_rows += 1
        for token in row["labels"]:
            yaml_status[str(token)] = row["status"]

    if regex_rows != 1:
        raise AssertionError(f"expected exactly one label_regex row (GST), found {regex_rows}")
    if not GST_KEY_RE.match("GST(18%)"):
        raise AssertionError("GST_KEY_RE no longer matches the GST label shape")

    code_status = _code_label_status()

    only_yaml = sorted(set(yaml_status) - set(code_status))
    only_code = sorted(set(code_status) - set(yaml_status))
    disagree = sorted(
        (k, yaml_status[k], code_status[k])
        for k in set(yaml_status) & set(code_status)
        if yaml_status[k] != code_status[k]
    )

    if only_yaml or only_code or disagree:
        raise AssertionError(
            "adapters/fatura.py has drifted from the reviewed mapping/fatura_field_map.yaml.\n"
            f"  in YAML, unknown to code : {only_yaml}\n"
            f"  in code, absent from YAML: {only_code}\n"
            f"  status disagreement      : {disagree}\n"
            "Amend the YAML (with a CHANGELOG entry) or the code. Do not loosen this check."
        )


def check_parsers_exist() -> None:
    """Every parser named in the rule table must be registered."""
    named = {t.parser for rules in SCALAR_RULES.values() for t in rules}
    named |= {t.parser for t in GST_RULES}
    named |= {parser for _, parser in PARTY_SUBFIELD.values()}
    unknown = sorted(named - set(P.known()))
    if unknown:
        raise AssertionError(f"rule table names unregistered parsers: {unknown}")
