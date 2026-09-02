"""Template for a new dataset adapter. Copy, rename, implement the three methods.

    cp datasets/_template.py datasets/cord_receipt.py

Read datasets/base.py first — the three obligations in DatasetAdapter.build's docstring are the
ones the FATURA adapter learned the hard way.
"""
from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Dict, Iterable, Set, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import BenchmarkRecord      # noqa: E402
from datasets.base import DatasetAdapter        # noqa: E402
from core.normalize import ABSENT               # noqa: E402

META_KEYS = {"file_name"}          # keys that are metadata, not annotations


def iter_labels(raw: Any) -> Iterable[Tuple[str, Any]]:
    """(label, value) pairs, flattened to the granularity the field map works at.
    Used by step 1 to inventory what the dataset actually contains."""
    for k, v in (raw or {}).items():
        if k in META_KEYS:
            continue
        if isinstance(v, dict):
            for sk, sv in v.items():
                yield f"{k}.{sk}", sv
        else:
            yield k, v


class TemplateAdapter(DatasetAdapter):
    source_name = "CHANGE_ME"

    def iter_raw(self) -> Iterable[tuple]:
        """Yield (doc_id, raw_annotation) for every document."""
        for path in sorted((self.root / "annotations").glob("*.json")):
            yield path.stem, json.loads(path.read_text(encoding="utf-8"))

    def build(self, doc_id: str, raw: Any) -> BenchmarkRecord:
        gt: Dict[str, Any] = {}
        annotated: Set[str] = set()
        excluded: Dict[str, str] = {}

        # ------------------------------------------------------------------ 1. map the labels
        # For each label the map covers, parse its value(s) into canonical form and write them
        # to the schema path(s). One label may produce SEVERAL paths — see docs/01_KEY_MAPPING.md.
        #
        #   gt["totals.totalIncludingTax"] = parse_money(raw["total"])
        #   annotated.add("totals.totalIncludingTax")

        # ------------------------------------------------------------------ 2. absences
        # A path this dataset CAN annotate but did not, for this document, is a scoreable
        # absence — and a value there is a hallucination:
        #
        #   gt[path] = ABSENT ; annotated.add(path)
        #
        # A path the dataset cannot speak to at all goes in NEITHER: leaving it out of
        # annotated_fields is what excludes it from every denominator.
        #
        # Decide per label whether absence is authoritative, by eyeballing five documents where
        # the label is missing. If it is, you get a real hallucination metric — rare and valuable.

        # ------------------------------------------------------------------ 3. exclusions
        # Anything dropped for THIS document, with a reason that gets published:
        #
        #   excluded["totals.taxAmount"] = "multi-rate tax; the schema holds one tax line"

        rec = BenchmarkRecord(
            doc_id=doc_id,
            source_dataset=self.source_name,
            doc_type=self.spec.name,
            # RELATIVE to the dataset root. Never absolute — ground truth is built on one
            # machine and run on another.
            image_path=str(pathlib.PurePosixPath("images") / f"{doc_id}.jpg"),
            # The unit that REPEATS. Intervals resample this, not documents. For a synthetic
            # corpus it is the template; for a natural one it may be the vendor, the source
            # batch, or the document itself. Getting it wrong makes intervals too narrow.
            cluster_id=doc_id,
            keyset_id=self.keyset_id(l for l, _ in iter_labels(raw)),
            gt=gt,
            annotated_fields=annotated,
            excluded_fields=excluded,
            meta={"labels": sorted(l for l, _ in iter_labels(raw))},
        )
        rec.validate()          # fails loudly on a malformed record
        return rec

    def check_contract(self, map_path: str) -> None:
        """Assert this module still agrees with the reviewed field map.

        Compare label -> mapped/unmapped on both sides using the map's machine-readable
        `labels:` lists. Raise AssertionError on ANY drift — this gates ground-truth builds.
        Resolve a failure by amending one side with a changelog entry, never by loosening
        the check. See datasets/fatura_invoice.py::check_against_yaml for a worked version.
        """
        raise NotImplementedError("implement check_contract — it is what keeps the map honest")
