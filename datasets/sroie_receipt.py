"""SROIE -> BenchmarkRecord.

Adapter for the SROIE dataset.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import BenchmarkRecord                      # noqa: E402
from datasets.base import DatasetAdapter                        # noqa: E402
from datasets import sroie_parsers as P                         # noqa: E402
from core.normalize import ABSENT                               # noqa: E402


class Target:
    def __init__(self, path: str, parser: str, kwargs: Tuple[Tuple[str, Any], ...] = ()):
        self.path = path
        self.parser = parser
        self.kwargs = kwargs

    def run(self, raw: Any, key: str) -> Optional[str]:
        return P.get(self.parser)(raw, key=key, **dict(self.kwargs))


def T(path: str, parser: str, **kwargs) -> Target:
    return Target(path, parser, tuple(sorted(kwargs.items())))


SCALAR_RULES: Dict[str, List[Target]] = {
    "company": [T("parties.seller.name", "passthrough_strip")],
    # txnDateISO uses SROIE's OWN date parser, not FATURA's. fatura_parsers.date_to_iso is
    # strict to "%d-%b-%Y" and parsed 5 of these 626 dates, which is why the field was reported
    # at n=5. See datasets/sroie_parsers.py for the day-first locale decision and the 215
    # order-ambiguous dates it still declines to read.
    "date":    [T("receiptInfo.txnDate", "passthrough_strip"),
                T("receiptInfo.txnDateISO", "sroie_date_to_iso")],
    "address": [T("parties.seller.addressStructured", "address_whole")],
    "total":   [T("totals.totalIncludingTax", "money_amount")],
}

UNMAPPED_LABELS = set()


def iter_labels(raw: dict):
    if "key_value_pairs" in raw:
        kv = raw["key_value_pairs"]
        for k, v in kv.items():
            yield k, v
    else:
        for k, v in raw.items():
            yield k, v


class SroieAdapter(DatasetAdapter):
    source_name = "SROIE"

    def __init__(self, dataset_root: str, spec=None, *, doc_type: str = "receipt"):
        import doctypes as _dt
        self.spec = spec or _dt.get(doc_type)
        self.root = pathlib.Path(dataset_root).expanduser()
        self.ann_dir = self.root / "annotations"
        self.img_dir = self.root / "images"
        self.doc_type = self.spec.name
        if not self.ann_dir.is_dir():
            raise FileNotFoundError(f"no annotations under {self.root}")

    def check_contract(self, map_path: str) -> None:
        import yaml as _yaml
        doc = _yaml.safe_load(open(map_path, encoding="utf-8"))
        yaml_status = {}
        for row in doc.get("mappings", []):
            if "labels" in row:
                for token in row["labels"]:
                    yaml_status[str(token)] = row["status"]
                    
        code_status = {label: "mapped" for label in SCALAR_RULES}
        for label in UNMAPPED_LABELS:
            code_status[label] = "unmapped"
            
        only_yaml = sorted(set(yaml_status) - set(code_status))
        only_code = sorted(set(code_status) - set(yaml_status))
        disagree = sorted(
            (k, yaml_status[k], code_status[k])
            for k in set(yaml_status) & set(code_status)
            if yaml_status[k] != code_status[k]
        )

        if only_yaml or only_code or disagree:
            raise AssertionError(
                "adapters/sroie.py has drifted from the reviewed map.\n"
                f"  in YAML, unknown to code : {only_yaml}\n"
                f"  in code, absent from YAML: {only_code}\n"
                f"  status disagreement      : {disagree}\n"
            )

    def _iter_raw(self) -> Iterable[Tuple[str, dict]]:
        for path in sorted(self.ann_dir.glob("*.json")):
            with open(path, encoding="utf-8") as fh:
                yield path.stem, json.load(fh)

    def iter_raw(self):
        return self._iter_raw()

    def build(self, name: str, data: dict) -> BenchmarkRecord:
        labels = set(data.get("key_value_pairs", {}).keys())

        # For SROIE, absent label means absent from page
        vocab_paths = set()
        for label, targets in SCALAR_RULES.items():
            for t in targets:
                vocab_paths.add(t.path)

        writes: Dict[str, List[Tuple[str, bool, Optional[str]]]] = {}
        excluded: Dict[str, str] = {}
        annotated: Set[str] = set()

        def record(path: str, source: str, raw: Any, value: Optional[str]) -> None:
            raw_empty = raw is None or (isinstance(raw, str) and not raw.strip())
            writes.setdefault(path, []).append((source, raw_empty, value))

        kv = data.get("key_value_pairs", {})
        # A handful of SROIE images are published twice under different ids, and five of those
        # pairs carry DIFFERENT ground truth for the same pixels. scripts/prepare_sroie_full.py
        # folds each image to one document and resolves the disagreements that are merely
        # notational ("RM7.42" vs "7.42"); what it cannot resolve it hands over here, under
        # `key_value_variants`, rather than picking an annotator to believe. Each surviving
        # value goes through the SAME target parsers as the primary one and is recorded as its
        # own source, so the conflict rule below excludes the path on its own terms -- and a
        # variant that the parsers happen to agree on costs nothing.
        variants = data.get("key_value_variants", {})
        for label in labels:
            if label not in SCALAR_RULES:
                continue
            sources = [(label, kv.get(label))]
            sources += [(f"{label}~dup{i}", alt)
                        for i, alt in enumerate(variants.get(label, []), start=1)]
            for source, raw in sources:
                for target in SCALAR_RULES[label]:
                    if target.path == "receiptInfo.txnDateISO":
                        # Map contract 1.13.1, rule 3b. The ISO date is the one target whose
                        # value can depend on the REST of the page: where the numeric field
                        # leaves the day/month order open, a month printed in words elsewhere
                        # on the receipt settles it. Passed the whole record rather than the
                        # label's value alone.
                        value = P.resolve_date_with_page(raw, data.get("ocr_lines"))
                    else:
                        value = target.run(raw, label)
                    record(target.path, source, raw, value)

        gt: Dict[str, Any] = {}

        for path in sorted(vocab_paths):
            candidates = writes.get(path, [])
            values = [v for _, _, v in candidates if v is not None]
            unparseable = [s for s, raw_empty, v in candidates if not raw_empty and v is None]

            if values:
                if len(set(values)) > 1:
                    excluded[path] = "conflicting GT labels"
                    continue
                gt[path] = values[0]
            elif unparseable:
                if path == "receiptInfo.txnDateISO":
                    # Two distinct findings, published apart: a date whose day/month order the
                    # page does not settle, versus one we genuinely cannot read. Map contract
                    # 1.13; see datasets/sroie_parsers.py::date_exclusion_reason.
                    excluded[path] = P.date_exclusion_reason(kv.get("date"))
                else:
                    excluded[path] = (
                        f"GT value present but unparseable "
                        f"({', '.join(sorted(set(unparseable)))})")
                continue
            else:
                gt[path] = ABSENT
            annotated.add(path)

        # Build image path: "images/{name}.jpg" usually
        img_name = data.get("image_file", f"images/{name}.jpg")
        
        record_obj = BenchmarkRecord(
            doc_id=name,
            source_dataset="SROIE",
            doc_type=self.doc_type,
            image_path=img_name,
            # One cluster per document. True by construction rather than by assumption: the
            # published corpus ships 15 images twice under different ids, and the prep script
            # folds each to a single record, so no two documents here share a page.
            cluster_id=name,
            keyset_id=hashlib.sha1("|".join(sorted(labels)).encode("utf-8")).hexdigest()[:10],
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            # split is the ORIGINAL ICDAR train/test boundary, reconstructed by image bytes
            # (the mirror's own 70/15/15 re-split puts six images on both sides of itself).
            # Absent on the legacy 626-document root, which is train-only.
            meta={"labels": sorted(labels), "split": data.get("split", "train")}
        )
        record_obj.validate()
        return record_obj

    def iter_records(self) -> Iterable[BenchmarkRecord]:
        for name, data in self._iter_raw():
            yield self.build(name, data)
