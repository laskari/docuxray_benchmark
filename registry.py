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

    variant_of: str = ""
    """This entry is the SAME corpus as another, stored under a different value policy.

    A variant may share its base's `root_config_key` and `map_path` -- same files, same reviewed
    label contract -- but never its `gt_subdir`, because the stored ground truth differs. The
    two uniqueness tests in tests/test_registry_paths.py exempt a DECLARED variant and still
    fail on an undeclared collision, which is the failure they were written for (DocILE reading
    FATURA's images). Declaring it is the whole point: an alias nobody wrote down is the bug.

    Used by `cord_verbatim`, which stores CORD's strings as downloaded while `cord` parses them
    into amounts. See the value_modes block in mapping/cord_receipt.map.yaml.
    """

    requires_numeric_source: str = ""
    """This dataset's stored values are only comparable against ONE key of a NumericValue.

    `fatura_verbatim` stores the printed span, so '(-) 4.35' parses to -4.35 while the product
    ships +4.35 (invoice_postprocessor._normalize_totals runs abs()). Scoring it against
    `normalizedValue` puts totals.discountTotal at 0.00% for a sign convention. Declared here so
    core.metrics refuses the pairing instead of publishing the artefact.

    Empty means the dataset is comparable either way -- `cord_verbatim` deliberately is, and
    documents why in its map's value_modes block."""

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

    # The SAME corpus, stored two ways -- see datasets/fatura_invoice.py::FaturaVerbatimAdapter.
    #   fatura           parsed    'TOTAL: "408.61 USD"' -> '408.61'    scores the AMOUNT
    #   fatura_verbatim  verbatim  'TOTAL: "408.61 USD"' -> '408.61 USD' scores the TRANSCRIPTION
    # Two entries rather than a flag, so a run can be scored both ways from cache and neither
    # number can be mistaken for the other. They share the reviewed map and the known-GT-error
    # list: the label -> path contract is identical and only the value policy differs.
    # SCORE IT AGAINST originalValue -- verbatim GT and normalizedValue are incompatible.
    "fatura_verbatim": DatasetEntry(
        name="fatura_verbatim", doc_type="invoice",
        module="fatura_invoice", cls="FaturaVerbatimAdapter",
        root_config_key="dataset",
        map_path="mapping/fatura_invoice.map.yaml",
        gt_errors_path="mapping/fatura_invoice.gt_errors.json",
        gt_subdir="fatura_verbatim",
        variant_of="fatura",
        requires_numeric_source="originalValue",
        extra_smoke_docs=("Template1_Instance0",),
    ),

    "cord": DatasetEntry(
        name="cord", doc_type="receipt",
        module="cord_receipt", cls="CordAdapter",
        root_config_key="dataset_cord",
        map_path="mapping/cord_receipt.map.yaml",
        gt_subdir="cord",
        # CORD-v2 ships train/dev/test and the adapter builds all three into ONE corpus,
        # tagging each record with meta['split']. The map is piloted on dev and the reported
        # numbers come from test; train is held in reserve. Split-aware plans are emitted by
        # scripts/plan_by_split.py, not by a second registry entry that could drift.
    ),
    # The SAME corpus, stored two ways, because CORD publishes strings and not numbers.
    #   cord           parsed   '24.000' -> 24000.00   scores the shipped AMOUNT
    #   cord_verbatim  verbatim '24.000' -> '24.000'   scores the TRANSCRIPTION
    # Two entries rather than a flag on one, so a run can be scored both ways from cache and
    # neither number can be mistaken for the other. They share the reviewed map: the label ->
    # status contract is identical and only the value policy differs.
    "cord_verbatim": DatasetEntry(
        name="cord_verbatim", doc_type="receipt",
        module="cord_receipt", cls="CordVerbatimAdapter",
        root_config_key="dataset_cord",
        map_path="mapping/cord_receipt.map.yaml",
        gt_subdir="cord_verbatim",
        variant_of="cord",
    ),

    "sroie": DatasetEntry(
        name="sroie", doc_type="receipt",
        module="sroie_receipt", cls="SroieAdapter",
        root_config_key="dataset_sroie",
        map_path="mapping/sroie_receipt.map.yaml",
        gt_subdir="sroie",
    ),
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
        if e.doc_type != doc_type:
            # A named dataset used to be returned whatever doc type was asked for, so
            # `--dataset fatura` against run.doc_type: receipt built the path
            # gt/receipt/fatura/ and surfaced three frames later as a FileNotFoundError on a
            # directory that can never exist. The registry knows which doc type each dataset
            # belongs to; say so here instead.
            raise KeyError(
                f"dataset {name!r} is a {e.doc_type!r} dataset, but doc_type {doc_type!r} was "
                f"requested. Pass --doc-type {e.doc_type} (or set run.doc_type: {e.doc_type} "
                f"in config.yaml). {doc_type!r} datasets: "
                f"{sorted(x.name for x in REGISTRY.values() if x.doc_type == doc_type) or 'none'}")
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
