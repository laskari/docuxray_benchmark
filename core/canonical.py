"""BenchmarkRecord -- the single interchange type between adapters and the scorer.

Three-state null semantics (Benchmarking_plan.md section 6) are the highest-risk rule in the
harness, so they are encoded structurally rather than left to convention:

    path in gt, value is not ABSENT   -> (i)   annotated and present. Counts toward recall.
    path in gt, value is ABSENT       -> (ii)  annotated as absent. A model emission here is a
                                               hallucination candidate. Counts toward the
                                               correct-null line, never toward a headline.
    path not in annotated_fields      -> (iii) the dataset cannot tell us. Excluded from EVERY
                                               denominator.

Most schema paths are state (iii) on most FATURA documents -- 27 of 72 InvoiceData leaves are
never annotated at all -- so counting "model correctly emitted null" as a win would inflate
every number past recognition. That is what the split above exists to prevent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.normalize import ABSENT, _Absent  # noqa: E402

_ABSENT_WIRE = "__ABSENT__"


@dataclass
class BenchmarkRecord:
    doc_id: str
    source_dataset: str
    doc_type: str                       # 'invoice' | 'receipt'
    image_path: str

    # Clustering identity. 200 instances of one template are ONE layout observation, so every
    # metric reports n_templates beside n_docs and intervals come from a cluster bootstrap
    # resampling templates. cluster_id is what the bootstrap resamples.
    cluster_id: str                     # = template_id for FATURA
    keyset_id: str                      # hash of the label set; the pilot samples one per keyset

    gt: Dict[str, Any] = field(default_factory=dict)
    annotated_fields: Set[str] = field(default_factory=set)

    # Paths dropped for THIS document and why -- a GT defect, or a schema limitation. Published
    # so a reader can see exactly what was excluded and reconstruct the denominators.
    excluded_fields: Dict[str, str] = field(default_factory=dict)

    meta: Dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- invariants

    def validate(self) -> None:
        """Fail loudly on a malformed record. Silent skips are how coverage quietly shrinks."""
        stray = set(self.gt) - self.annotated_fields
        if stray:
            raise ValueError(
                f"{self.doc_id}: gt carries {sorted(stray)[:5]} which are not in "
                f"annotated_fields. Every scored value must be declared annotated."
            )
        overlap = self.annotated_fields & set(self.excluded_fields)
        if overlap:
            raise ValueError(
                f"{self.doc_id}: {sorted(overlap)[:5]} are both annotated and excluded."
            )
        if not self.cluster_id:
            raise ValueError(f"{self.doc_id}: cluster_id is required for cluster bootstrapping.")

    # ---------------------------------------------------------------- convenience

    @property
    def present_fields(self) -> Set[str]:
        """State (i): annotated with a value. The recall denominator."""
        return {p for p, v in self.gt.items() if not isinstance(v, _Absent)}

    @property
    def absent_fields(self) -> Set[str]:
        """State (ii): annotated as absent. The hallucination denominator."""
        return {p for p, v in self.gt.items() if isinstance(v, _Absent)}

    # ---------------------------------------------------------------- wire format

    def to_json(self) -> str:
        d = asdict(self)
        d["gt"] = {k: (_ABSENT_WIRE if isinstance(v, _Absent) else v) for k, v in self.gt.items()}
        d["annotated_fields"] = sorted(self.annotated_fields)
        return json.dumps(d, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, line: str) -> "BenchmarkRecord":
        d = json.loads(line)
        d["gt"] = {k: (ABSENT if v == _ABSENT_WIRE else v) for k, v in d["gt"].items()}
        d["annotated_fields"] = set(d["annotated_fields"])
        return cls(**d)


def write_jsonl(records: List[BenchmarkRecord], path: str) -> int:
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(r.to_json() + "\n")
    return len(records)


def read_jsonl(path: str) -> List[BenchmarkRecord]:
    with open(path, encoding="utf-8") as fh:
        return [BenchmarkRecord.from_json(line) for line in fh if line.strip()]
