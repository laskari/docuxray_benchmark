"""Which datasets exist, and which document type each one benchmarks.

One entry per (dataset x doc type). Adding a dataset means adding one entry here and one module
under datasets/ -- nothing in core/ changes.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Tuple

import doctypes


@dataclass(frozen=True)
class DatasetEntry:
    name: str
    doc_type: str
    module: str                # datasets/<module>.py
    cls: str                   # adapter class inside it
    root_config_key: str       # which config.yaml paths.* entry holds the data
    map_path: str              # the reviewed field map
    gt_errors_path: str = ""   # known ground-truth defects, if any

    extra_smoke_docs: Tuple[str, ...] = ()
    """Documents to force into the smoke plan because they exercise a branch that
    one-per-cluster sampling cannot reach. Each one needs a stated reason -- this is a
    deliberate exception to the sampling rule, not a convenience."""

    def load(self, root: str | None = None):
        from config import load as load_cfg
        cfg = load_cfg()
        mod = importlib.import_module(f"datasets.{self.module}")
        adapter_cls = getattr(mod, self.cls)
        return adapter_cls(root or cfg["paths"][self.root_config_key],
                           doctypes.get(self.doc_type))

    def iter_labels(self, raw) -> Iterable[Tuple[str, object]]:
        mod = importlib.import_module(f"datasets.{self.module}")
        return mod.iter_labels(raw)


REGISTRY: Dict[str, DatasetEntry] = {
    "fatura": DatasetEntry(
        name="fatura", doc_type="invoice",
        module="fatura_invoice", cls="FaturaAdapter",
        root_config_key="dataset",
        map_path="mapping/fatura_invoice.map.yaml",
        gt_errors_path="mapping/fatura_invoice.gt_errors.json",
        # Instance0 documents are excluded from normal sampling (FATURA seeded each template's
        # seller block from its own Instance0 buyer, so 27 of 31 carry a duplicate-address
        # quirk found in ZERO other documents). One is forced in so that branch is still
        # exercised; this one was additionally verified against its page image.
        extra_smoke_docs=("Template1_Instance0",),
    ),
    # ---- to add CORD receipts, uncomment and implement datasets/cord_receipt.py -----------
    # "cord": DatasetEntry(
    #     name="cord", doc_type="receipt",
    #     module="cord_receipt", cls="CordAdapter",
    #     root_config_key="dataset_cord",
    #     map_path="mapping/cord_receipt.map.yaml",
    # ),
}


def dataset_for(doc_type: str, name: str | None = None) -> DatasetEntry:
    if name:
        e = REGISTRY.get(name)
        if not e:
            raise KeyError(f"unknown dataset {name!r}; known: {sorted(REGISTRY)}")
        return e
    matches = [e for e in REGISTRY.values() if e.doc_type == doc_type]
    if not matches:
        raise KeyError(f"no dataset registered for doc_type {doc_type!r}. "
                       f"Add one in registry.py — see docs/NEW_DOCTYPE_CHECKLIST.md")
    return matches[0]
