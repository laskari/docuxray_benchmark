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
from datasets import fatura_parsers as P                        # noqa: E402
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
    "date":    [T("receiptInfo.txnDate", "passthrough_strip"),
                T("receiptInfo.txnDateISO", "date_to_iso")],
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
        for label in labels:
            if label not in SCALAR_RULES:
                continue
            raw = kv.get(label)
            for target in SCALAR_RULES[label]:
                record(target.path, label, raw, target.run(raw, label))

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
                excluded[path] = f"GT value present but unparseable ({', '.join(sorted(set(unparseable)))})"
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
            cluster_id=name, # SROIE documents are distinct, so each is its own cluster
            keyset_id=hashlib.sha1("|".join(sorted(labels)).encode("utf-8")).hexdigest()[:10],
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            meta={"labels": sorted(labels)}
        )
        record_obj.validate()
        return record_obj

    def iter_records(self) -> Iterable[BenchmarkRecord]:
        for name, data in self._iter_raw():
            yield self.build(name, data)
