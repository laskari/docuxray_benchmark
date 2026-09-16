"""CORD-v2 -> BenchmarkRecord.

Reads the per-document JSON that scripts/prepare_cord_v2.py materialises from
`naver-clova-ix/cord-v2`, one folder per split:

    <root>/<split>/annotations/<id>.json
    <root>/<split>/images/<id>.<ext>

THREE THINGS THIS ADAPTER DECIDES, each of which would be wrong if left to a default:

1. DOC IDS ARE SPLIT-QUALIFIED. CORD restarts its image_id at 1 in every split, so 99 ids
   collide between any two of them. A bare id would make train/1, dev/1 and test/1 the same
   document to the cache, the plans and the ground-truth index. Ids are therefore 'test-1'.

2. ALL THREE SPLITS ARE BUILT, AND meta['split'] CARRIES WHICH. Ground truth is cheap and
   held-out measurement is the point of using v2: the map is piloted on dev, the reported
   numbers come from test, and train is the reserve. Keeping one corpus with a split label
   beats three registry entries that can drift apart.

3. ABSENCE IS NOT ANNOTATED. CORD records what a human marked on the page; a missing label
   means the annotator did not mark it, not that the receipt lacks the fact. So a path with no
   parseable value is left OUT of annotated_fields entirely (state iii) rather than written as
   ABSENT. That forfeits a hallucination denominator, which the map states plainly -- inventing
   one from a non-authoritative absence would be worse than not having it.

VERBATIM MODE. `CordAdapter(..., verbatim=True)` stores CORD's string exactly as downloaded
instead of an amount. CORD publishes STRINGS -- '24.000', 'Rp 38.000', '@24.000' -- and never
says what they are worth, so the two modes answer two different questions and neither is a
substitute for the other:

    parsed    '24.000' -> 24000.00   "did the pipeline produce the right NUMBER?"
    verbatim  '24.000' -> '24.000'   "did the pipeline TRANSCRIBE what is printed?"

The trade is concrete and measured. In verbatim mode the harness's own numeric rule reads GT
'24.000' as 24.00 and the product's normalizedValue 24.0 as 24.00, so they MATCH -- and a
shipped value that is wrong by a factor of 1000 scores as correct. Verbatim measures the
extractor's reading and is blind to the normaliser; parsed measures the shipped amount. Run
both; publish both.

The header is absent by construction: CORD's taxonomy has no store-name, address or date class
and the source images are blurred there. Every parties.* and receiptInfo.* path is simply never
annotated, so the generic scorer drops them from every denominator with no special casing.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import BenchmarkRecord          # noqa: E402
from datasets.base import DatasetAdapter            # noqa: E402
from datasets import cord_parsers as P              # noqa: E402
from core.rows import TIEBREAK_KEYS                 # noqa: E402

SPLITS: Tuple[str, ...] = ("train", "dev", "test")

#: Trailing rate on a printed tax caption: 'PB1 10%' -> ('PB1', '10%').
_CAPTION_RATE = re.compile(r"^(?P<name>.*?)[\s:]*(?P<rate>\d+(?:[.,]\d+)?\s*%)\s*$")

# ------------------------------------------------------------------ line items
#: GT row key -> the schema leaves a prediction may legitimately use for it. A tuple with more
#: than one entry is a UNION: the best-matching leaf wins and core/rows.py records which.
LINE_ITEM_TARGETS: Dict[str, Tuple[str, ...]] = {
    "description":     ("description",),
    "itemCode":        ("itemCode",),
    "quantity":        ("quantity",),
    "unitPrice":       ("unitPrice",),
    # CORD does not state whether the printed line amount is tax-inclusive, and Indonesian
    # receipts vary. A model that routed it to ExcludingTax has not got the NUMBER wrong.
    "lineTotal":       ("lineTotalIncludingTax", "lineTotalExcludingTax"),
    "discountAmount":  ("discountAmount",),
    "discountPercent": ("discountPercent",),
}

#: CORD row key -> (GT row key, parser). menu.price and menu.itemsubtotal both feed lineTotal;
#: price wins where both are present.
ROW_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("nm",            "description",     "cord_text"),
    ("num",           "itemCode",        "passthrough_strip"),
    ("cnt",           "quantity",        "cord_qty"),
    ("unitprice",     "unitPrice",       "cord_money"),
    ("price",         "lineTotal",       "cord_money"),
    ("itemsubtotal",  "lineTotal",       "cord_money"),      # fallback, 4 documents
    ("discountprice", "discountAmount",  "cord_money_abs"),
    ("discountprice", "discountPercent", "cord_percent"),
)

#: A keyed totals row is identified by its printed label, never by position -- a one-row
#: section matched positionally would score whatever the model happened to emit first.
#:
#: `key` is the row's IDENTITY and is deliberately NOT a scored cell. core/rows.py aligns on it
#: (via row_match_keys), so a row can only align when its key already matched at ANLS >= 0.8 --
#: which made the key cell 46/46, 23/23 and 8/8 correct on the test-100 run. Scoring it was
#: re-reporting the alignment decision as a free win, and those 77 tautological cells were
#: diluting the value accuracy they sat beside. A key the model gets WRONG still costs it: the
#: row fails to align and lands in row recall, where it belongs.
KEYED_TARGETS: Dict[str, Tuple[str, ...]] = {"value": ("value",)}

#: The tax section additionally carries a rate, because ReceiptTotals.taxes[] has a field for
#: one and the printed caption often IS "<name> <rate>" -- 'PB1 10%'. Splitting it is what the
#: schema asks for: the model emits key 'PB1' and percentage '10%', and matching the whole
#: caption against the key scored a correct answer as a row miss (ANLS 0.43).
#: totals.otherCharges deliberately does NOT do this: it has no percentage field, so 'SVC CHG
#: 6%' is the charge's whole name and splitting it would break an alignment that works.
TAX_TARGETS: Dict[str, Tuple[str, ...]] = {
    "value": ("value",), "percentage": ("percentage",),
}

#: CORD totals label -> (schema list path, the key the row is filed under, parser)
KEYED_RULES: Dict[str, Tuple[str, str, str]] = {
    "sub_total.subtotal_price": ("totals.subtotal",     "subtotal",      "cord_money"),
    "sub_total.tax_price":      ("totals.taxes",        "tax",           "cord_money"),
    "sub_total.service_price":  ("totals.otherCharges", "service",       "cord_money"),
    "sub_total.othersvc_price": ("totals.otherCharges", "other service", "cord_money"),
}

#: CORD totals label -> (scalar schema path, parser)
SCALAR_RULES: Dict[str, Tuple[str, str]] = {
    "total.total_price":         ("totals.totalIncludingTax", "cord_money"),
    "total.cashprice":           ("totals.cash",              "cord_money"),
    "total.changeprice":         ("totals.change",            "cord_money"),
    "sub_total.discount_price":  ("totals.discountTotal",     "cord_money_abs"),
}

#: CORD annotates it; ReceiptData has no field for it. Scored nowhere, but kept visible -- the
#: contract check fails if this set and the reviewed map's `unmapped` rows disagree.
UNMAPPED_LABELS: Set[str] = {
    "menu.sub", "menu.etc", "menu.vatyn",
    "void_menu.nm", "void_menu.price",
    "sub_total.etc",
    "total.creditcardprice", "total.emoneyprice",
    "total.menuqty_cnt", "total.menutype_cnt", "total.total_etc",
}

ROW_TARGETS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "lineItems": LINE_ITEM_TARGETS,
    "totals.subtotal": KEYED_TARGETS,
    "totals.taxes": TAX_TARGETS,
    "totals.otherCharges": KEYED_TARGETS,
}
#: Alignment keys, in preference order. `keyAlt` carries the FULL printed caption where `key`
#: has had a trailing rate stripped, because models disagree about which form belongs in the
#: label: on 'PB1 10%' the extractor emitted 'PB1' and put the rate in `percentage`, while on
#: 'TAX 10.00 %' it emitted the caption whole. Offering both forms means alignment no longer
#: depends on that choice -- the same trick the DocILE adapter used for `descriptionAlt`, and
#: for the same reason. Neither is a SCORED cell; they are identity only.
ROW_MATCH_KEYS: Dict[str, Tuple[str, ...]] = {
    "lineItems": ("description", "itemCode"),
    "totals.subtotal": ("key", "keyAlt"),
    "totals.taxes": ("key", "keyAlt"),
    "totals.otherCharges": ("key", "keyAlt"),
}
ROW_TEXT_LEAVES: Dict[str, Tuple[str, ...]] = {
    "lineItems": ("description", "itemCode"),
    # The prediction side has one label leaf; both GT forms are compared against it.
    "totals.subtotal": ("key",),
    "totals.taxes": ("key",),
    "totals.otherCharges": ("key",),
}


# ------------------------------------------------------------------ label inventory
def _menu_rows(parse: Dict[str, Any]) -> List[Dict[str, Any]]:
    menu = parse.get("menu") or []
    if isinstance(menu, dict):
        menu = [menu]
    return [r for r in menu if isinstance(r, dict)]


def _section(parse: Dict[str, Any], name: str) -> Dict[str, Any]:
    """CORD emits a section as a dict, or occasionally as a list of dicts when the receipt
    prints the block twice. Merging is safe here because the per-KEY collision is what matters
    and a repeated key arrives as a list value, which the parsers refuse by design."""
    node = parse.get(name)
    if isinstance(node, dict):
        return node
    if isinstance(node, list):
        merged: Dict[str, Any] = {}
        for entry in node:
            if isinstance(entry, dict):
                merged.update(entry)
        return merged
    return {}


def printed_captions(raw: Any) -> Dict[str, str]:
    """category -> the caption PRINTED beside the value, from valid_line's is_key words.

    gt_parse drops these (that is what is_key is for), but a keyed totals row is identified by
    its printed label and nothing else. Scoring one against an invented English key measured
    whether the receipt happened to say the word "subtotal": the smoke run aligned 3 of 7
    subtotals, 2 of 5 taxes and 1 of 3 service charges, and EVERY aligned row was 100% correct
    -- the model had read 'SUBTTL', 'PB1' and 'SVC CHG 6%' exactly as printed while the map
    demanded 'subtotal', 'tax' and 'service'.

    scripts/prepare_cord_v2.py keeps valid_line for precisely this kind of question, so the
    caption is recovered rather than guessed. Where a receipt prints no caption at all the
    canonical name is used as a fallback and the map says so.
    """
    out: Dict[str, str] = {}
    for line in (raw or {}).get("valid_line") or []:
        cat = line.get("category") or ""
        caption = " ".join(w.get("text", "") for w in (line.get("words") or [])
                           if w.get("is_key")).strip()
        if cat and caption and cat not in out:
            out[cat] = caption
    return out


def keyed_occurrences(raw: Any) -> Dict[str, List[Tuple[str, str]]]:
    """category -> [(printed caption, printed value)] for every OCCURRENCE, in page order.

    gt_parse collapses a category the receipt printed twice into a list of bare values with no
    captions, which is unusable: the two lines are different lines. valid_line keeps them
    separate, with the is_key words that name each one -- so this reads the occurrences from
    there and gt_parse is used only as a fallback.

    Measured on all 1,000 documents: 9 print more than one `sub_total.subtotal_price`. 7 are the
    SAME amount twice, and test-13 shows why -- it prints 'TOTAL 46.636' and then 'DPP 46.636',
    DPP being Dasar Pengenaan Pajak, the Indonesian tax base. A restatement, not a component.
    """
    out: Dict[str, List[Tuple[str, str]]] = {}
    for line in (raw or {}).get("valid_line") or []:
        cat = line.get("category") or ""
        if not cat:
            continue
        words = line.get("words") or []
        caption = " ".join(w.get("text", "") for w in words if w.get("is_key")).strip()
        value = " ".join(w.get("text", "") for w in words if not w.get("is_key")).strip()
        if value:
            out.setdefault(cat, []).append((caption, value))
    return out


def iter_labels(raw: Any) -> Iterable[Tuple[str, Any]]:
    """(label, value) at the granularity the field map works at: 'section.key'.

    Line-item keys are yielded ONCE per distinct key present across the rows, not once per row,
    so the keyset id describes the document's SCHEMA rather than how long its table is.
    """
    parse = (raw or {}).get("gt_parse") or {}
    seen: Set[str] = set()
    for row in _menu_rows(parse):
        for k in row:
            if k.startswith("_") or f"menu.{k}" in seen:
                continue
            seen.add(f"menu.{k}")
            yield f"menu.{k}", None
    for name in ("void_menu", "sub_total", "total"):
        for k, v in _section(parse, name).items():
            if k.startswith("_"):
                continue
            yield f"{name}.{k}", v


class CordAdapter(DatasetAdapter):
    source_name = "cord-v2"

    #: exposed for core/rows.py — which schema leaves each GT row key may be scored against
    row_targets = ROW_TARGETS
    row_match_keys = ROW_MATCH_KEYS
    row_text_leaves = ROW_TEXT_LEAVES

    def __init__(self, root, spec=None, *, splits: Tuple[str, ...] = SPLITS,
                 verbatim: bool = False):
        """verbatim=True stores CORD's STRING exactly as downloaded, with no numeric
        interpretation by us -- see VERBATIM MODE in the module docstring."""
        import doctypes as _dt
        super().__init__(root, spec or _dt.get("receipt"))
        self.verbatim = bool(verbatim)
        self.source_name = "cord-v2-verbatim" if verbatim else "cord-v2"
        self.splits = tuple(splits)
        present = [s for s in self.splits if (self.root / s / "annotations").is_dir()]
        if not present:
            raise FileNotFoundError(
                f"no <split>/annotations under {self.root}. Run "
                f"scripts/prepare_cord_v2.py --out {self.root} first — CORD-v2 ships as "
                f"parquet with the images embedded, not as files.")
        self.splits = tuple(present)

    # ------------------------------------------------------------------ raw
    def iter_raw(self) -> Iterable[Tuple[str, dict]]:
        for split in self.splits:
            for path in sorted((self.root / split / "annotations").glob("*.json"),
                               key=lambda p: (len(p.stem), p.stem)):
                data = json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("split", split)
                data.setdefault("_stem", path.stem)
                yield f"{split}-{path.stem}", data

    # ------------------------------------------------------------------ value policy
    def _value(self, raw, parser_name: str, leaf: str):
        """The single place that decides what gets STORED for a raw CORD value.

        Two corpora, two questions, and the difference is entirely here:

          value mode (default)  parse the string into an amount -- 'Rp 38.000' -> 38000.00.
                                Asks: does the pipeline produce the right NUMBER? This is the
                                only mode that can see a normalisation defect, because it is
                                the only one holding an opinion about what the digits mean.

          verbatim mode         store the string as downloaded -- 'Rp 38.000' stays 'Rp 38.000'.
                                Asks: did the pipeline TRANSCRIBE what is printed? No
                                interpretation of ours enters the ground truth at all.

        A list value is refused in BOTH modes: CORD emitted the same category twice and which
        one is meant is genuinely unknown, so it becomes a published exclusion either way.
        """
        if raw is None or isinstance(raw, list):
            return None
        if not self.verbatim:
            return P.get(parser_name)(raw, key=leaf)
        s = re.sub(r"\s+", " ", str(raw)).strip()
        return s or None

    # ------------------------------------------------------------------ rows
    def _line_items(self, parse, excluded: Dict[str, str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for idx, src in enumerate(_menu_rows(parse)):
            row: Dict[str, Any] = {}
            for cord_key, gt_key, parser_name in ROW_RULES:
                if cord_key not in src or gt_key in row:
                    continue                       # price beats itemsubtotal, both -> lineTotal
                raw = src[cord_key]
                value = self._value(raw, parser_name, cord_key)
                if value is not None:
                    row[gt_key] = value
                elif not (parser_name == "cord_percent" and "discountAmount" in row) and \
                        not (parser_name == "cord_money_abs" and cord_key == "discountprice"
                             and isinstance(raw, str) and "%" in raw):
                    # A genuine refusal, not the amount/percent branch declining its turn.
                    excluded[f"lineItems[{idx}].{gt_key}"] = _reason(raw)
            if not row.get("description") and not row.get("itemCode"):
                # core/rows.py aligns on identifying TEXT. A row with neither cannot be matched
                # to a prediction, so scoring its cells would compare against an arbitrary
                # partner. Dropped with a reason rather than counted as an unfound row.
                excluded[f"lineItems[{idx}]"] = "row has no description or itemCode to align on"
                continue
            out.append(row)
        return out

    @staticmethod
    def _drop_unattributable_cells(rows: List[Dict[str, Any]], excluded: Dict[str, str]) -> None:
        """Remove cells that alignment cannot attribute to a specific row.

        core/rows.py aligns on identifying TEXT and breaks ties on TIEBREAK_KEYS. Where two rows
        of a document agree on every one of those AND differ somewhere else, the aligner is free
        to pair them either way -- correctly so, since nothing distinguishes them -- and the
        differing cell then scores by coin flip. CORD has 5 such documents (10 rows, 0.39% of
        2,573), every one a line repeated with a 100% discount on the second copy:

            SALMON PESTO  1  85,500
            SALMON PESTO  1  85,500   discount 85,500

        The discount belongs to ONE of those rows and the annotation does not say which. Scoring
        it would report the aligner's arbitrary choice as a model result; the honest move is to
        drop the cell from both rows with a published reason and keep the rows themselves, which
        are still perfectly scoreable on every other cell.

        Detected rather than hard-coded, and keyed off the aligner's own constants, so a change
        to TIEBREAK_KEYS cannot leave this silently wrong.
        """
        align = tuple(ROW_MATCH_KEYS["lineItems"]) + tuple(TIEBREAK_KEYS)
        groups: Dict[tuple, List[int]] = {}
        for i, row in enumerate(rows):
            groups.setdefault(tuple(str(row.get(k)) for k in align), []).append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            others = {k for i in idxs for k in rows[i] if k not in align}
            for key in sorted(others):
                if len({rows[i].get(key) for i in idxs}) == 1:
                    continue
                for i in idxs:
                    rows[i].pop(key, None)
                    excluded[f"lineItems[{i}].{key}"] = (
                        "rows indistinguishable to the aligner (identical description, "
                        "itemCode and tie-break cells); this cell cannot be attributed to one")

    def _keyed_rows(self, parse, raw, excluded: Dict[str, str]) -> Dict[str, List[Dict[str, Any]]]:
        """One row per DISTINCT printed amount, labelled by its own caption.

        WHY NOT SUM MULTIPLE SUBTOTALS. Tested on every document that prints more than one
        (9 corpus-wide, 1 in the test split). Against the printed grand total, the SUM matched
        in 0 of 8 and the LARGEST in 7 of 8: 7 of the 9 are literally the same amount twice
        ('20,000', '20,000'), so summing doubles it. test-13, verified against its image, prints
        the subtotal 46.636 twice -- once as TOTAL and once as DPP, the tax base -- with line
        items 43.636 + 3.000 = 46.636 and 10% tax bringing it to the 51.300 grand total. A sum
        of 93,272 corresponds to nothing on the page.

        WHY NOT COLLAPSE TO ONE ROW EITHER. ReceiptTotals.subtotal is a LIST of {key, value}
        precisely so several printed lines can coexist, and 2 of the 9 carry genuinely different
        amounts. So: one row per distinct value, keyed by the caption the receipt printed.

        Exact duplicates are deduplicated rather than emitted twice. The second line restates a
        fact already recorded, and requiring a model to echo it would make row recall a measure
        of transcription completeness rather than of reading the receipt. The first caption wins,
        being the one the receipt leads with, and the dropped caption is recorded as an exclusion
        so the decision is visible rather than silent.
        """
        rows: Dict[str, List[Dict[str, Any]]] = {}
        occ = keyed_occurrences(raw)
        sub, tot = _section(parse, "sub_total"), _section(parse, "total")

        for label, (list_path, fallback, parser_name) in KEYED_RULES.items():
            section, leaf = label.split(".", 1)
            seen: Dict[str, str] = {}          # parsed value -> the caption that claimed it

            items = occ.get(label)
            if not items:
                # No word-level annotation for this category: fall back to gt_parse, which may
                # hold a bare list with no captions.
                v = (sub if section == "sub_total" else tot).get(leaf)
                if v is None:
                    continue
                items = [("", x) for x in (v if isinstance(v, list) else [v])]

            for caption, printed in items:
                value = self._value(printed, parser_name, leaf)
                key = caption.strip() or fallback
                if value is None:
                    excluded[f"{list_path}[{key}]"] = _reason(printed)
                    continue
                if value in seen:
                    excluded[f"{list_path}[{key}]"] = (
                        f"the same amount is printed again under a different caption "
                        f"(already recorded as {seen[value]!r}); a restatement, not a component")
                    continue
                seen[value] = key

                row: Dict[str, Any] = {"key": key, "value": value}
                # A caption ending in a rate is offered BOTH ways for alignment, and on the tax
                # section the rate also becomes a scored cell, because taxes[] has a field for
                # one. Other sections have no percentage field, so there the rate is only ever
                # part of the label.
                m = _CAPTION_RATE.match(key)
                if m and m.group("name").strip():
                    row["key"] = m.group("name").strip()
                    row["keyAlt"] = key
                    if list_path == "totals.taxes":
                        pct = (m.group("rate").strip() if self.verbatim
                               else P.get("cord_percent")(m.group("rate"), key=leaf))
                        if pct is not None:
                            row["percentage"] = pct
                rows.setdefault(list_path, []).append(row)
        return rows

    # ------------------------------------------------------------------ build
    def build(self, doc_id: str, raw: Any) -> BenchmarkRecord:
        parse = (raw or {}).get("gt_parse") or {}
        split = raw.get("split") or (raw.get("meta") or {}).get("split") or "train"
        gt: Dict[str, Any] = {}
        annotated: Set[str] = set()
        excluded: Dict[str, str] = {}

        rows = self._line_items(parse, excluded)
        self._drop_unattributable_cells(rows, excluded)
        if rows:
            gt["lineItems"] = rows
            annotated.add("lineItems")
        else:
            excluded["lineItems"] = "document has no alignable menu rows (0 GT rows)"

        for list_path, keyed in self._keyed_rows(parse, raw, excluded).items():
            gt[list_path] = keyed
            annotated.add(list_path)

        sub, tot = _section(parse, "sub_total"), _section(parse, "total")
        for label, (path, parser_name) in SCALAR_RULES.items():
            section, leaf = label.split(".", 1)
            src = sub if section == "sub_total" else tot
            if leaf not in src:
                # Absence is NOT authoritative on CORD (see the module docstring and the map's
                # absence_policy), so the path is left out of annotated_fields entirely rather
                # than written as ABSENT. No hallucination denominator, stated rather than faked.
                continue
            value = self._value(src[leaf], parser_name, leaf)
            if value is None:
                excluded[path] = _reason(src[leaf])
                continue
            gt[path] = value
            annotated.add(path)

        labels = sorted(l for l, _ in iter_labels(raw))
        stem = raw.get("_stem") or doc_id.split("-", 1)[-1]
        image_file = raw.get("image_file") or f"images/{stem}.png"

        rec = BenchmarkRecord(
            doc_id=doc_id,
            source_dataset=self.source_name,
            doc_type=self.spec.name,
            # RELATIVE to the dataset root, and split-qualified because the root holds all three.
            image_path=str(pathlib.PurePosixPath(split) / image_file),
            # CORD has no templates and no repeated vendors: the document IS the repeating unit,
            # so effective N equals the document count and intervals bootstrap over documents.
            cluster_id=doc_id,
            keyset_id=self.keyset_id(labels),
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            meta={
                "labels": labels,
                "split": split,
                "value_mode": "verbatim" if self.verbatim else "parsed",
                "n_gt_rows": len(rows),
                "gt_parse_source": raw.get("gt_parse_source"),
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
        """Assert this module still agrees with the reviewed field map.

        Compares label -> mapped/unmapped on both sides using the map's machine-readable
        `labels:` lists, and additionally checks that the map's declared list sections match the
        ones this adapter actually populates. Raise on ANY drift: resolve by amending one side
        with a CHANGELOG entry, never by loosening the check.
        """
        import yaml as _yaml

        doc = _yaml.safe_load(open(map_path, encoding="utf-8"))
        yaml_status: Dict[str, str] = {}
        for row in doc.get("mappings", []):
            if "labels" not in row:
                raise AssertionError(
                    f"map row {row.get('label')!r} has no machine-readable `labels:` list; "
                    f"the contract check cannot verify it.")
            for token in row["labels"]:
                yaml_status[str(token)] = row["status"]

        code_status = {label: "mapped" for label in
                       list(SCALAR_RULES) + list(KEYED_RULES)}
        for cord_key, _gt_key, _parser in ROW_RULES:
            code_status[f"menu.{cord_key}"] = "mapped"
        for label in UNMAPPED_LABELS:
            code_status[label] = "unmapped"

        only_yaml = sorted(set(yaml_status) - set(code_status))
        only_code = sorted(set(code_status) - set(yaml_status))
        disagree = sorted((k, yaml_status[k], code_status[k])
                          for k in set(yaml_status) & set(code_status)
                          if yaml_status[k] != code_status[k])

        yaml_sections = sorted(s["path"] for s in doc.get("list_sections", []))
        code_sections = sorted(ROW_TARGETS)
        section_drift = yaml_sections != code_sections

        if only_yaml or only_code or disagree or section_drift:
            raise AssertionError(
                "datasets/cord_receipt.py has drifted from the reviewed "
                "mapping/cord_receipt.map.yaml.\n"
                f"  in YAML, unknown to code : {only_yaml}\n"
                f"  in code, absent from YAML: {only_code}\n"
                f"  status disagreement      : {disagree}\n"
                f"  list sections YAML/code  : {yaml_sections} / {code_sections}\n"
                "Amend the YAML (with a CHANGELOG entry) or the code. Do not loosen this check.")

        # The map is only honest if every path it names is a real schema leaf.
        tsv = _ROOT / "schema" / "receipt_leaf_paths.tsv"
        if tsv.exists():
            leaves = {line.split("\t")[0] for line in
                      tsv.read_text(encoding="utf-8").splitlines() if line.strip()}
            declared = {p for row in doc.get("mappings", [])
                        for t in (row.get("targets") or []) for p in [t["path"]]}
            declared |= set(doc.get("not_annotated", {}).get("paths") or [])
            unknown = sorted(p for p in declared if p not in leaves)
            if unknown:
                raise AssertionError(
                    f"the map names schema paths that are not in {tsv.name}: {unknown}. "
                    f"Regenerate the TSV or fix the map — a target that is not a real leaf can "
                    f"never be scored, and would silently contribute nothing.")


def _reason(raw: Any) -> str:
    if isinstance(raw, list):
        return "GT value present but ambiguous (CORD annotated the same category twice)"
    return f"GT value present but unparseable ({str(raw)[:40]!r})"


class CordVerbatimAdapter(CordAdapter):
    """CORD-v2 with ground truth stored EXACTLY as downloaded -- no numeric interpretation.

    A separate class rather than a constructor argument because registry.DatasetEntry.load()
    passes only (root, spec): the value policy has to be part of the adapter's identity, and
    making it one also means `gt/receipt/cord_verbatim/` can never be built by the parsed
    adapter or vice versa.
    """

    def __init__(self, root, spec=None, **kw):
        kw.pop("verbatim", None)
        super().__init__(root, spec, verbatim=True, **kw)
