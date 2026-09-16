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
    """A DECLARED variant may share its base's root -- it is the same corpus, stored under a
    different value policy. An UNDECLARED collision is the DocILE/FATURA failure and still
    fails here."""
    keys = [e.root_config_key for e in REGISTRY.values() if not e.variant_of]
    assert len(keys) == len(set(keys)), (
        f"two non-variant datasets share a root_config_key {keys} — one would silently read "
        f"the other's images, which is exactly the DocILE/FATURA failure. If they really are "
        f"the same corpus, say so with variant_of=")
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
    """Same rule as the root path: a declared variant shares its base's reviewed map, because
    the label -> status contract is identical and only the stored value differs. Two unrelated
    datasets sharing one map would mean one of them was never reviewed."""
    maps = [e.map_path for e in REGISTRY.values() if not e.variant_of]
    assert len(maps) == len(set(maps)), (
        f"two non-variant datasets share a reviewed map {maps}; one of them has no contract "
        f"of its own. If they are the same corpus, declare variant_of=")
    root = pathlib.Path(__file__).resolve().parent.parent
    for e in REGISTRY.values():
        assert (root / e.map_path).exists(), f"{e.name}: {e.map_path} does not exist"


def test_a_variant_declares_a_real_base_and_its_own_ground_truth():
    """The exemption above is only safe if a variant is a variant of something real, of the
    same doc type, and cannot overwrite its base's ground truth."""
    for e in REGISTRY.values():
        if not e.variant_of:
            continue
        base = REGISTRY.get(e.variant_of)
        assert base is not None, f"{e.name} declares variant_of={e.variant_of!r}, which is not a dataset"
        assert base.doc_type == e.doc_type, (
            f"{e.name} is a {e.doc_type} dataset but its base {base.name} is {base.doc_type}")
        assert not base.variant_of, f"{e.name} is a variant of a variant ({base.name})"
        assert e.gt_subdir and e.gt_subdir != base.gt_subdir, (
            f"{e.name} shares gt_subdir {e.gt_subdir!r} with its base {base.name} — the two "
            f"store DIFFERENT ground truth and one build would overwrite the other")


def test_naming_a_dataset_returns_that_dataset():
    """An orphaned `for e in invoices:` fragment sat here referring to a name no longer
    defined, so this file raised NameError instead of asserting anything. Restored, and
    generalised past invoices."""
    for e in REGISTRY.values():
        assert dataset_for(e.doc_type, e.name) is e


def test_a_dataset_asked_for_under_the_wrong_doc_type_is_rejected():
    """`--dataset fatura` against run.doc_type: receipt used to return the FATURA entry
    regardless, build the path gt/receipt/fatura/, and surface three frames later as a
    FileNotFoundError on a directory that can never exist. The registry knows which doc type
    each dataset belongs to, so it says so."""
    wrong = [(e, other) for e in REGISTRY.values()
             for other in {x.doc_type for x in REGISTRY.values()} if other != e.doc_type]
    if not wrong:
        pytest.skip("only one doc type is registered")
    for e, other in wrong:
        with pytest.raises(KeyError) as exc:
            dataset_for(other, e.name)
        message = str(exc.value)
        assert e.doc_type in message and e.name in message
        assert "--doc-type" in message


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
