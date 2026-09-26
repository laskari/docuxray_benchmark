#!/usr/bin/env python3
"""Materialise the FULL SROIE corpus into the on-disk layout the benchmark adapters expect.

    <out>/images/<doc_id>.jpg
    <out>/annotations/<doc_id>.json
    <out>/duplicates.json      the dedup audit trail
    <out>/manifest.json        counts, provenance, and the old-id -> doc_id map

    python3 scripts/prepare_sroie_full.py --zip ~/Downloads/sroie-dataset.zip \
        --out ../../Benchmark/sroie_data/SROIE_FULL
    python3 scripts/prepare_sroie_full.py --src /tmp/sroie/raw --out ../../Benchmark/sroie_data/SROIE_FULL

WHAT THIS REPLACES. The benchmark has been running on 626 documents taken from the
zzzDavid/ICDAR-2019-SROIE GitHub repo, which publishes the task-3 TRAIN split only, renumbered
"000".."625". Those numbers are the repo's, not SROIE's, and they carry no provenance. The
Kaggle mirror `dattrinh12/sroie-dataset` (itself a repackaging of `urbikn/sroie-datasetv2`)
publishes all 973 task-3 files under their ORIGINAL ICDAR names, so this script switches the
corpus to those names and picks up the 337 documents that were missing.

    626 files / 621 distinct images   ->   973 files / 958 distinct images

Verified against the old root before the switch: every one of the 621 distinct images the
benchmark already had is byte-identical to a file here, so this is a strict superset and no
document is lost. The response cache is keyed on sha256(image bytes) (core/runner.py::
_cache_key), NOT on doc_id, so renaming costs no re-extraction -- only the 337 new images are
unpaid for.

THREE THINGS THE MIRROR GETS WRONG, AND WHAT IS DONE ABOUT THEM.

1. IT SHIPS DUPLICATES. 15 pairs of files are byte-identical images published under two IDs
   (X51005453801 / X51009453801, and 13 more). Kept as-is they would be extracted twice, paid
   for twice, and counted twice in every denominator. ONE RECORD PER DISTINCT IMAGE is written;
   the folded-away IDs are recorded in duplicates.json rather than dropped silently.

2. ITS SPLITS LEAK. The mirror re-split the corpus 70/15/15 into train/eval/test, and five
   images land in two of its splits at once -- so its "test" set is not held out from its
   train set and cannot be reported from. The ORIGINAL ICDAR boundary is reconstructed instead,
   by asking which images the task-3 train split contains (--train-ref, the 626-file root, or
   the persisted mapping/sroie_original_split.json). That comes out at 621 train / 337 test
   distinct images with NO image on both sides. meta['split'] carries it; the mirror's own
   split is kept alongside as provenance only.

3. DUPLICATE COPIES SOMETIMES DISAGREE ABOUT THE GROUND TRUTH. Of the 15 pairs, 5 carry
   different `entities` for the same pixels. Three of those disagreements are notational and
   vanish under the benchmark's own comparison normalisation -- "RM7.42" vs "7.42",
   "NO: 28, JALAN" vs "NO: 28,JALAN". Those are resolved here, to the canonical copy's
   spelling, and every one is logged. The remaining two are substantive -- one company name
   ("BEMED (SP) SDN BHD" vs "BEMED (SP) SDN. BHD.") and one address carrying an extra outlet
   line -- and the corpus does not get to guess which annotator was right: the disagreeing
   values are written to `key_value_variants` and the adapter excludes that path for that
   document with reason "conflicting GT labels". Two field-level exclusions in 958 documents.

ocr_lines ARE PARSED LINE-BASED, NOT AS CSV. A box file row is
`x1,y1,x2,y2,x3,y3,x4,y4,text`, and the text is raw: it may itself contain commas and stray
double quotes. Splitting on the first 8 commas and taking the rest verbatim is therefore the
correct read. The old 626-file packaging used a quote-aware CSV reader instead, which treats a
lone `"` as opening a quoted field and swallows every following row until the next one -- on
doc 381 that merged 17 printed lines into a single entry (79 lines where there are 96). 615 of
the 621 carried-over documents come out byte-identical to the old annotation either way; the 6
that differ are that quoting bug plus three character-level revisions upstream (`OR`/`Or`,
`.`/`·`, `:`/`l`). ocr_lines feeds one thing only -- the month-name evidence that settles a
numeric date's day/month order (map contract 1.13.1, datasets/sroie_parsers.py) -- so recovering
more page text can only help it, and none of the 6 changed the months visible on the page.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import shutil
import sys
import zipfile

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core import normalize as N                                  # noqa: E402

SPLITS = ("train", "eval", "test")
LABELS = ("company", "date", "address", "total")
WELL_FORMED_ID = re.compile(r"^X\d{11}$")
SPLIT_MAP_PATH = _ROOT / "mapping" / "sroie_original_split.json"


# --------------------------------------------------------------------------- reading
def read_text(path: pathlib.Path) -> str:
    """SROIE box files are mostly UTF-8; one (X51006619503) is not. Decode rather than fail:
    the fallback only ever affects the OCR text, never a coordinate or a ground-truth value."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def box_lines(path: pathlib.Path) -> list:
    """The printed text of each detected box, in file order. See the module docstring for why
    this is a line-based split on the first 8 commas and not a csv.reader."""
    out = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",", 8)
        if len(parts) < 9:
            continue
        text = parts[8].strip()
        if text:
            out.append(text)
    return out


def sha1_of(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def load_entities(path: pathlib.Path) -> dict:
    """entities/<id>.txt is already clean per-document JSON with the four task-3 keys, so this
    is a read and not a parse. Kept verbatim -- the map and the parsers own every value
    decision downstream, and re-spelling anything here would put a second policy in the data."""
    return json.loads(read_text(path))


# --------------------------------------------------------------------------- dedup
def canonical_id(ids: list) -> str:
    """Which of a duplicate group's IDs the surviving record is filed under.

    Stated as a rule so the corpus is reproducible: prefer an ID of SROIE's own form
    (X + 11 digits), then the lexicographically smallest. The preference is not cosmetic --
    one duplicate is published as X510056849111, twelve digits, and picking it would file a
    document under an ID that matches nothing upstream.
    """
    well_formed = sorted(i for i in ids if WELL_FORMED_ID.match(i))
    return well_formed[0] if well_formed else sorted(ids)[0]


def comparably_equal(label: str, a: str, b: str) -> bool:
    """Do two spellings of the same label mean the same thing to the SCORER?

    Asked with the benchmark's own comparison normalisers, so this cannot drift from how the
    values are actually judged: whatever the scorer would call equal is a notational variant
    and is resolved to the canonical copy, and whatever it would call different is a real
    disagreement between two annotators about the same pixels and is escalated, not picked.
    """
    if a is None or b is None:
        return a == b
    if label == "address":
        return N.normalise_address_compare(a) == N.normalise_address_compare(b)
    if label == "total":
        return N.money_to_str(N.normalise_money(a)) == N.money_to_str(N.normalise_money(b))
    if label == "company":
        return N.normalise_string(a) == N.normalise_string(b)
    return (a or "").strip() == (b or "").strip()


# --------------------------------------------------------------------------- original split
def original_train_ids(src: pathlib.Path, train_ref: pathlib.Path | None) -> tuple:
    """The ICDAR task-3 TRAIN membership, as a set of this corpus's doc IDs.

    Recovered by image bytes from the 626-file root the benchmark already had, because that
    root IS the published train split. Persisted to mapping/sroie_original_split.json on the
    way through so a later run reproduces the same boundary without that root on disk.
    """
    by_hash = collections.defaultdict(list)
    for split in SPLITS:
        for img in sorted((src / split / "img").glob("*.jpg")):
            by_hash[sha1_of(img)].append(img.stem)

    if train_ref and train_ref.is_dir():
        ref = {sha1_of(p) for p in sorted((train_ref / "images").glob("*.jpg"))}
        if not ref:
            raise SystemExit(f"--train-ref {train_ref} holds no images/*.jpg")
        train = {i for h, ids in by_hash.items() if h in ref for i in ids}
        missing = ref - set(by_hash)
        if missing:
            raise SystemExit(
                f"{len(missing)} of the {len(ref)} reference images are absent from --src. "
                "This mirror is not a superset of the corpus in use; refusing to build a "
                "corpus that silently drops documents.")
        return train, "reconstructed from --train-ref by image bytes"

    if SPLIT_MAP_PATH.exists():
        saved = json.loads(SPLIT_MAP_PATH.read_text(encoding="utf-8"))
        return set(saved["train_ids"]), f"read from {SPLIT_MAP_PATH.name}"

    raise SystemExit(
        "cannot determine the original ICDAR train/test boundary: pass --train-ref pointing at "
        f"the 626-document root, or provide {SPLIT_MAP_PATH}. The mirror's own train/eval/test "
        "split leaks across itself and must not be used as the boundary.")


# --------------------------------------------------------------------------- build
def build(src: pathlib.Path, out: pathlib.Path, train_ref: pathlib.Path | None,
          old_root: pathlib.Path | None) -> dict:
    for split in SPLITS:
        for sub in ("img", "box", "entities"):
            if not (src / split / sub).is_dir():
                raise SystemExit(f"expected {src / split / sub} to exist")

    train_ids, split_provenance = original_train_ids(src, train_ref)

    # group every published file by the bytes of its image
    groups = collections.defaultdict(list)
    for split in SPLITS:
        for img in sorted((src / split / "img").glob("*.jpg")):
            groups[sha1_of(img)].append((split, img.stem))

    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "annotations").mkdir(parents=True, exist_ok=True)

    dup_report, resolved_variants, escalated = [], [], []
    n_docs = collections.Counter()

    for digest, members in sorted(groups.items(), key=lambda kv: canonical_id([m[1] for m in kv[1]])):
        ids = [stem for _, stem in members]
        doc_id = canonical_id(ids)
        split_of = {stem: sp for sp, stem in members}
        canon_split = split_of[doc_id]

        entities = {stem: load_entities(src / split_of[stem] / "entities" / f"{stem}.txt")
                    for stem in ids}
        kv = dict(entities[doc_id])
        variants: dict = {}

        for label in LABELS:
            mine = kv.get(label)
            for other in ids:
                if other == doc_id:
                    continue
                theirs = entities[other].get(label)
                if theirs == mine:
                    continue
                if comparably_equal(label, mine, theirs):
                    resolved_variants.append(
                        {"doc_id": doc_id, "other_id": other, "label": label,
                         "kept": mine, "dropped": theirs,
                         "why": "equal under the benchmark's comparison normalisation"})
                else:
                    variants.setdefault(label, [])
                    if theirs not in variants[label]:
                        variants[label].append(theirs)
                    escalated.append(
                        {"doc_id": doc_id, "other_id": other, "label": label,
                         "values": [mine, theirs]})

        original_split = "train" if doc_id in train_ids else "test"
        n_docs[original_split] += 1

        record = {
            "image_id": doc_id,
            "image_file": f"images/{doc_id}.jpg",
            "language": "en",
            "split": original_split,
            "key_value_pairs": kv,
            "ocr_lines": box_lines(src / canon_split / "box" / f"{doc_id}.txt"),
            "provenance": {
                "source": "kaggle:dattrinh12/sroie-dataset",
                "mirror_split": canon_split,
                "image_sha1": digest,
                "duplicate_ids": sorted(i for i in ids if i != doc_id),
            },
        }
        if variants:
            # Read by datasets/sroie_receipt.py, which feeds each variant through the same
            # target parsers as the primary value and lets the adapter's existing conflict
            # rule exclude the path. The corpus records the disagreement; it does not settle it.
            record["key_value_variants"] = variants

        shutil.copy2(src / canon_split / "img" / f"{doc_id}.jpg", out / "images" / f"{doc_id}.jpg")
        (out / "annotations" / f"{doc_id}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        if len(ids) > 1:
            dup_report.append({
                "doc_id": doc_id, "image_sha1": digest,
                "folded": sorted(i for i in ids if i != doc_id),
                "mirror_splits": sorted({sp for sp, _ in members}),
                "spans_mirror_splits": len({sp for sp, _ in members}) > 1,
            })

    # old numeric id -> original name, so a pre-existing plan or report can still be read
    old_map = {}
    if old_root and (old_root / "images").is_dir():
        by_hash = {d: canonical_id([s for _, s in m]) for d, m in groups.items()}
        for p in sorted((old_root / "images").glob("*.jpg")):
            hit = by_hash.get(sha1_of(p))
            if hit:
                old_map[p.stem] = hit

    SPLIT_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_MAP_PATH.write_text(json.dumps(
        {"note": "ICDAR-2019 SROIE task-3 TRAIN membership, by document id. Written by "
                 "scripts/prepare_sroie_full.py so the original split survives without the "
                 "626-document root on disk. The Kaggle mirror's own split leaks and is not "
                 "this.",
         "provenance": split_provenance,
         "n_train_ids": len(train_ids),
         "train_ids": sorted(train_ids)}, indent=2) + "\n", encoding="utf-8")

    (out / "duplicates.json").write_text(json.dumps({
        "note": "Byte-identical images published under more than one id. One record per "
                "distinct image survives, filed under canonical_id(); the rest are listed "
                "here. `resolved_variants` are ground-truth spellings that differed between "
                "copies but are equal to the scorer. `escalated` are real disagreements: the "
                "adapter excludes those paths rather than picking a side.",
        "n_groups": len(dup_report),
        "n_folded_ids": sum(len(d["folded"]) for d in dup_report),
        "groups": dup_report,
        "resolved_variants": resolved_variants,
        "escalated": escalated,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    manifest = {
        "dataset": "SROIE (ICDAR-2019 task 3), full corpus",
        "source": "kaggle:dattrinh12/sroie-dataset",
        "built_by": "scripts/prepare_sroie_full.py",
        "n_published_files": sum(len(v) for v in groups.values()),
        "n_documents": sum(n_docs.values()),
        "n_duplicate_groups": len(dup_report),
        "split_provenance": split_provenance,
        "splits": dict(n_docs),
        "gt_conflicts_resolved": len(resolved_variants),
        "gt_conflicts_escalated": len(escalated),
        "old_id_to_doc_id": old_map,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", type=pathlib.Path, help="the downloaded Kaggle zip")
    src.add_argument("--src", type=pathlib.Path,
                     help="an already-extracted tree of <split>/{img,box,entities}")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    ap.add_argument("--train-ref", type=pathlib.Path,
                    default=pathlib.Path("../../Benchmark/sroie_data/SROIE"),
                    help="the 626-document root, read ONLY to recover the original ICDAR "
                         "train/test boundary and the old-id map")
    ap.add_argument("--extract-to", type=pathlib.Path,
                    help="where to unpack --zip (default: <out>/../_sroie_src)")
    args = ap.parse_args()

    if args.zip:
        dest = args.extract_to or (args.out.parent / "_sroie_src")
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.zip) as zf:
            zf.extractall(dest)
        source = dest
    else:
        source = args.src

    train_ref = args.train_ref.expanduser() if args.train_ref else None
    old_root = train_ref
    manifest = build(source.expanduser(), args.out.expanduser(), train_ref, old_root)

    print(f"wrote {manifest['n_documents']} documents to {args.out}")
    print(f"  from {manifest['n_published_files']} published files "
          f"({manifest['n_duplicate_groups']} duplicate image groups folded)")
    print(f"  original ICDAR split ({manifest['split_provenance']}): {manifest['splits']}")
    print(f"  ground-truth conflicts: {manifest['gt_conflicts_resolved']} resolved as "
          f"notation, {manifest['gt_conflicts_escalated']} escalated to the adapter")


if __name__ == "__main__":
    main()
