"""Every registered dataset must resolve to ITS OWN root, ground truth and map.

Written after a real failure: `core/runner.py` read `cfg["paths"]["dataset"]` for the image
root while taking ground truth from `--dataset`, so a DocILE run looked for DocILE's images
under FATURA's root and reported all 100 as missing. The pre-flight check caught it, but the
message blamed the dataset rather than the lookup — the worst kind of bug, because the
diagnosis it hands you is wrong.

These tests are cheap and they close the whole class: any new dataset that forgets a config
path, reuses another dataset's root key, or collides on a ground-truth directory fails here.
"""
from __future__ import annotations

import pathlib

import pytest

import doctypes
from config import load
from registry import REGISTRY, dataset_for, gt_dir


@pytest.fixture(scope="module")
def cfg():
    return load()


def test_every_dataset_has_its_own_config_path(cfg):
    keys = [e.root_config_key for e in REGISTRY.values()]
    assert len(keys) == len(set(keys)), (
        f"two datasets share a root_config_key {keys} — one would silently read the other's "
        f"images, which is exactly the DocILE/FATURA failure")
    for e in REGISTRY.values():
        assert e.root_config_key in cfg["paths"], (
            f"{e.name} declares paths.{e.root_config_key}, which config.yaml does not define")


def test_every_dataset_has_its_own_ground_truth_directory():
    """Keyed by (doc type, dataset). Keyed by doc type alone — as it was — a second invoice
    dataset would overwrite the first's ground truth on build."""
    dirs = [gt_dir(e.doc_type, e.name) for e in REGISTRY.values()]
    assert len(dirs) == len(set(dirs)), f"ground-truth directories collide: {dirs}"
    for e in REGISTRY.values():
        assert e.gt_subdir, (
            f"{e.name} has no gt_subdir, so its ground truth would land in the shared "
            f"gt/{e.doc_type}/ directory and collide with every other dataset of that type")


def test_every_dataset_has_its_own_reviewed_map():
    maps = [e.map_path for e in REGISTRY.values()]
    assert len(maps) == len(set(maps))
    root = pathlib.Path(__file__).resolve().parent.parent
    for e in REGISTRY.values():
        assert (root / e.map_path).exists(), f"{e.name}: {e.map_path} does not exist"


def test_an_ambiguous_doc_type_raises_instead_of_guessing():
    """Two invoice datasets are registered. Returning the first would make the answer depend
    on dict insertion order, so a run could be scored against a dataset it did not measure."""
    invoices = [e for e in REGISTRY.values() if e.doc_type == "invoice"]
    assert len(invoices) > 1, "this test is only meaningful while a doc type has several"
    with pytest.raises(KeyError, match="name one explicitly"):
        dataset_for("invoice")
    for e in invoices:
        assert dataset_for("invoice", e.name) is e


def test_the_declared_doc_type_exists(cfg):
    for e in REGISTRY.values():
        doctypes.get(e.doc_type)          # raises on an unknown type


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_each_datasets_images_live_under_its_own_root(name, cfg):
    """The end-to-end assertion: resolve the root the way the runner does, then check the
    ground truth's own image paths against it."""
    from core.canonical import read_jsonl

    e = REGISTRY[name]
    root = pathlib.Path(cfg["paths"][e.root_config_key])
    gtp = gt_dir(e.doc_type, e.name) / "ground_truth.jsonl"
    if not root.is_dir() or not gtp.exists():
        pytest.skip(f"{name}: dataset root or ground truth not present here")
    recs = read_jsonl(str(gtp))[:25]
    missing = [r.doc_id for r in recs if not (root / r.image_path).exists()]
    assert not missing, (
        f"{name}: {len(missing)} of {len(recs)} sampled images absent under "
        f"paths.{e.root_config_key} ({root}) — either the path is wrong or it points at "
        f"another dataset")
