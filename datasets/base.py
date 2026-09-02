"""What a dataset must provide to be benchmarkable.

A dataset adapter turns whatever the source publishes into BenchmarkRecord objects. Everything
downstream -- sampling, running, scoring, reporting -- consumes only BenchmarkRecord and never
knows which dataset it came from.

To add a dataset, copy datasets/_template.py and implement the three methods below.
"""
from __future__ import annotations

import abc
import hashlib
import pathlib
from typing import Any, Dict, Iterable, Set

from doctypes import DocTypeSpec


class DatasetAdapter(abc.ABC):
    """Base for every dataset adapter."""

    #: short, stable, appears in every record and in the run manifest
    source_name: str = "unnamed"

    def __init__(self, root: str | pathlib.Path, spec: DocTypeSpec):
        self.root = pathlib.Path(root).expanduser()
        self.spec = spec
        if not self.root.is_dir():
            raise FileNotFoundError(f"dataset root does not exist: {self.root}")

    # -------------------------------------------------------------------- required

    @abc.abstractmethod
    def iter_raw(self) -> Iterable[tuple]:
        """Yield (doc_id, raw_annotation) for every document in the dataset."""

    @abc.abstractmethod
    def build(self, doc_id: str, raw: Any):
        """Turn one raw annotation into a BenchmarkRecord.

        Three obligations, all of which the invoice adapter learned the hard way:

        1. `image_path` MUST be relative to the dataset root. Ground truth is built on one
           machine and run on another; an absolute path silently breaks every run elsewhere.
        2. `annotated_fields` must contain EVERY path the dataset can speak to for this
           document -- present or absent. A path outside it is excluded from every denominator,
           which is what stops "the model correctly emitted null" from inflating the numbers.
        3. `cluster_id` must name the unit that repeats. Confidence intervals resample clusters,
           not documents. For a synthetic corpus that is the template; for a natural one it may
           be the vendor, the source batch, or the document itself.
        """

    @abc.abstractmethod
    def check_contract(self, map_path: str) -> None:
        """Assert the executable rule table still agrees with the reviewed field map.

        Raise AssertionError on any drift. This gates ground-truth builds, so a map edited
        without a matching code change stops the build instead of silently changing results.
        """

    # -------------------------------------------------------------------- provided

    def iter_records(self):
        for doc_id, raw in self.iter_raw():
            yield self.build(doc_id, raw)

    @staticmethod
    def keyset_id(labels: Iterable[str]) -> str:
        """Stable id for a set of annotated labels. Documents sharing a keyset share a schema
        variant, which is what the pilot samples across."""
        return hashlib.sha1("|".join(sorted(labels)).encode("utf-8")).hexdigest()[:10]

    def describe(self) -> Dict[str, Any]:
        return {"source": self.source_name, "doc_type": self.spec.name, "root": str(self.root)}
