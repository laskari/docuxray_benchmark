"""Which datasets exist, and which document type each one benchmarks.

One entry per (dataset x doc type). Adding a dataset means adding one entry here and one module
under datasets/ -- nothing in core/ changes.
"""
from __future__ import annotations

import importlib
import pathlib
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Tuple

import doctypes

_ROOT = pathlib.Path(__file__).resolve().parent


@dataclass(frozen=True)
class DatasetEntry:
    name: str
    doc_type: str
    module: str                # datasets/<module>.py
    cls: str                   # adapter class inside it
    root_config_key: str       # which config.yaml paths.* entry holds the data
    map_path: str              # the reviewed field map
    gt_errors_path: str = ""   # known ground-truth defects, if any

    gt_subdir: str = ""
    """Where this dataset's ground truth lives, under gt/<doc_type>/.

    Ground truth used to be keyed by DOC TYPE alone (gt/invoice/ground_truth.jsonl), which
    silently collides the moment a second invoice dataset exists: FATURA and DocILE100 are both
    doc_type 'invoice', and the second build would overwrite the first. Keyed by dataset as
    well, so one doc type can carry as many datasets as it needs.
    """

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
        gt_subdir="fatura",
        # Instance0 documents are excluded from normal sampling (FATURA seeded each template's
        # seller block from its own Instance0 buyer, so 27 of 31 carry a duplicate-address
        # quirk found in ZERO other documents). One is forced in so that branch is still
        # exercised; this one was additionally verified against its page image.
        extra_smoke_docs=("Template1_Instance0",),
    ),
    "docile100": DatasetEntry(
        name="docile100", doc_type="invoice",
        module="docile_invoice", cls="DocileAdapter",
        root_config_key="dataset_docile",
        map_path="mapping/docile_invoice.map.yaml",
        gt_subdir="docile100",
        # No forced smoke documents. FATURA needs one because its Instance0 files carry a
        # quirk that one-per-cluster sampling cannot reach; DocILE100's cluster IS the
        # document, so every document is already its own cluster and nothing is unreachable.
    ),
    # ---- to add CORD receipts, uncomment and implement datasets/cord_receipt.py -----------
    # "cord": DatasetEntry(
    #     name="cord", doc_type="receipt",
    #     module="cord_receipt", cls="CordAdapter",
    #     root_config_key="dataset_cord",
    #     map_path="mapping/cord_receipt.map.yaml",
    # ),
}


def gt_dir(doc_type: str, name: str | None = None) -> pathlib.Path:
    """Where this (doc type, dataset) pair's ground truth lives.

    The single place that answers the question. Four modules used to build the path inline,
    which is why adding a second invoice dataset was a four-file change with one silent
    collision in it.
    """
    return _ROOT / "gt" / doc_type / dataset_for(doc_type, name).gt_subdir


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
    if len(matches) > 1:
        # Silently taking the first would make the answer depend on dict insertion order, so a
        # run could score against a different dataset than the one it measured.
        raise KeyError(
            f"doc_type {doc_type!r} has {len(matches)} datasets "
            f"({sorted(e.name for e in matches)}); name one explicitly — set run.dataset in "
            f"config.yaml or pass --dataset.")
    return matches[0]
