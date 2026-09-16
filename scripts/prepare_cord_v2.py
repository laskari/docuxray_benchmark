#!/usr/bin/env python3
"""Materialise CORD-v2 into the on-disk layout the benchmark adapters expect.

CORD-v2 (clovaai/cord, released on HuggingFace as `naver-clova-ix/cord-v2`) ships as parquet
with the image bytes embedded and the annotation as a JSON *string*. The harness wants files:

    <out>/<split>/images/<doc_id>.png
    <out>/<split>/annotations/<doc_id>.json

    python3 scripts/prepare_cord_v2.py --out ../../Benchmark/data/CORD_v2
    python3 scripts/prepare_cord_v2.py --parquet-dir ~/Downloads/cord-v2 --out ../../Benchmark/data/CORD_v2

Two input routes, because huggingface.co is blocked by the egress policy on some machines:

  --hf          (default) datasets.load_dataset("naver-clova-ix/cord-v2")
  --parquet-dir a directory of the dataset's .parquet files, downloaded by hand

WHY v2 AND NOT THE FLATTENED COPY. The existing Benchmark/data/CORD folder is CORD's 800-document
train split with the annotation flattened to `menu_0_nm`-style keys. That flattening throws away
four things this script keeps, every one of which the benchmark needs:

  group_id       the authoritative line-item grouping. The flattened copy re-encodes it as an
                 index inside the key name, and uses an UNINDEXED key when a receipt has one
                 row -- two parsing branches instead of a field.
  sub_group_id   proper sub-item hierarchy (added in v2). Not recoverable from the flat form.
  is_key         which words are the printed LABEL ("TOTAL", "Subtotal") rather than the value.
                 Without it you get artifacts: the flat copy stores total_menuqty_cnt as "(2".
  row_id / quad  reading order and geometry, for adjudicating a disputed row alignment.

v2 also corrected mislabels present in v1, and carries the dev and test splits, so a held-out
measurement is possible at all.

The per-document JSON written here holds BOTH representations:

  {"image_id", "image_file", "split",
   "gt_parse":   the nested {menu:[...], sub_total:{...}, total:{...}} form -- what the adapter reads
   "valid_line": the raw word-level annotation      -- the audit trail, and the fallback if
                                                       gt_parse is absent for a document
   "meta", "roi", "repeating_symbol", "dontcare"}

gt_parse is derived by CORD itself, so reading it is not a re-derivation on our part. When it is
missing the script rebuilds an equivalent from valid_line by (group_id, sub_group_id, category)
and records `gt_parse_source: "rebuilt"` so no document silently mixes provenance.
"""
from __future__ import annotations

import argparse
import base64
import collections
import io
import json
import pathlib
import re
import sys

SPLIT_ALIASES = {"validation": "dev", "valid": "dev", "val": "dev"}
SPLIT_TOKENS = ("train", "validation", "valid", "val", "dev", "test")


def norm_split(name: str) -> str:
    return SPLIT_ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())


def split_from_path(path: pathlib.Path, root: pathlib.Path) -> str:
    """Best guess from the file path. HuggingFace names files data/train-00000-of-000NN.parquet,
    so the SPLIT IS IN THE FILENAME, not the parent directory -- reading the parent gives 'data'
    for every split and silently merges all 1,000 documents into one folder."""
    stem = path.stem.lower()
    for tok in SPLIT_TOKENS:
        if re.match(rf"^{tok}(\b|[-_.])", stem):
            return norm_split(tok)
    for part in reversed(path.relative_to(root).parts[:-1]):
        if norm_split(part) in ("train", "dev", "test"):
            return norm_split(part)
    return "unknown"


# --------------------------------------------------------------------------- gt_parse rebuild
def rebuild_gt_parse(valid_line):
    """Reconstruct CORD's nested parse from the word-level annotation.

    Only used when a record has no gt_parse. Mirrors CORD's own grouping rule: a line item is
    one group_id under the `menu` superclass; sub-items are the sub_group_ids under it.
    Label words (is_key true) are dropped -- they are the receipt's printed caption, not the
    value, and keeping them is what turns a quantity of 2 into "(2".
    """
    menus = collections.defaultdict(dict)
    subs = collections.defaultdict(lambda: collections.defaultdict(dict))
    flat = collections.defaultdict(dict)

    for line in valid_line or []:
        cat = line.get("category") or ""
        if "." not in cat:
            continue
        sup, sub = cat.split(".", 1)
        text = " ".join(w.get("text", "") for w in (line.get("words") or [])
                        if not w.get("is_key")).strip()
        if not text:
            continue
        gid = line.get("group_id", 0)
        sgid = line.get("sub_group_id")
        if sup == "menu":
            if sub.startswith("sub_") and sgid is not None:
                subs[gid][sgid][sub[4:]] = text
            else:
                menus[gid][sub] = text
        elif sup in ("sub_total", "subtotal", "total", "void_menu"):
            flat["sub_total" if sup in ("sub_total", "subtotal") else sup][sub] = text

    out = {}
    if menus or subs:
        rows = []
        for gid in sorted(set(menus) | set(subs)):
            row = dict(menus.get(gid, {}))
            if gid in subs:
                row["sub"] = [dict(subs[gid][s], _sub_group_id=s) for s in sorted(subs[gid])]
            row["_group_id"] = gid
            rows.append(row)
        out["menu"] = rows
    for k in ("sub_total", "total", "void_menu"):
        if flat.get(k):
            out[k] = dict(flat[k])
    return out


# --------------------------------------------------------------------------- input routes
def iter_hf():
    try:
        from datasets import load_dataset
    except ImportError:
        sys.exit("pip install datasets  (or use --parquet-dir if huggingface.co is blocked)")
    ds = load_dataset("naver-clova-ix/cord-v2")
    for split in ds:
        for i, row in enumerate(ds[split]):
            yield split, i, row


def iter_parquet(parquet_dir: pathlib.Path):
    try:
        import pyarrow.parquet as pq
    except ImportError:
        sys.exit("pip install pyarrow")
    files = sorted(parquet_dir.rglob("*.parquet"))
    if not files:
        sys.exit(f"no .parquet under {parquet_dir}")
    for f in files:
        hint = split_from_path(f, parquet_dir)
        tbl = pq.read_table(f)
        for i, row in enumerate(tbl.to_pylist()):
            yield hint, i, row


# --------------------------------------------------------------------------- image bytes
MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
         (b"GIF8", ".gif"), (b"BM", ".bmp"), (b"II*\x00", ".tif"), (b"MM\x00*", ".tif"))


def image_ext(blob: bytes) -> str:
    """From the bytes, not from an assumption. CORD ships JPEG originals and the HF export is
    PNG; writing the wrong extension makes the runner's pre-flight image check fail on a file
    that is perfectly readable."""
    for sig, ext in MAGIC:
        if blob.startswith(sig):
            return ext
    return ".png"


def image_bytes(value):
    """HF gives {'bytes':..., 'path':...}; a hand-built parquet may give raw bytes or base64."""
    if isinstance(value, dict):
        if value.get("bytes"):
            return value["bytes"]
        if value.get("path"):
            return pathlib.Path(value["path"]).read_bytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        return base64.b64decode(value)
    raise TypeError(f"cannot read image bytes from {type(value)}")


# --------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="destination root, e.g. ../../Benchmark/data/CORD_v2")
    ap.add_argument("--parquet-dir", default=None,
                    help="read local .parquet instead of huggingface.co")
    ap.add_argument("--splits", default="train,validation,test")
    a = ap.parse_args(argv)

    want = {SPLIT_ALIASES.get(s.strip(), s.strip()) for s in a.splits.split(",")}
    out_root = pathlib.Path(a.out).expanduser()
    source = iter_parquet(pathlib.Path(a.parquet_dir).expanduser()) if a.parquet_dir else iter_hf()

    counts = collections.Counter()
    rebuilt = collections.Counter()
    skipped = collections.Counter()
    unknown = collections.Counter()
    seen = set()
    for path_hint, idx, row in source:
        gt = row.get("ground_truth")
        gt = json.loads(gt) if isinstance(gt, str) else (gt or {})
        meta = gt.get("meta") or {}

        # meta.split is CORD's own answer and beats any path convention. The path hint is the
        # fallback, and a document that has neither is parked in 'unknown' rather than guessed
        # into a split -- a misfiled test document would silently contaminate the held-out set.
        split = norm_split(meta.get("split") or "") or norm_split(path_hint) or "unknown"
        if split == "unknown":
            unknown[path_hint] += 1
        if split not in want:
            skipped[split] += 1
            continue

        doc_id = str(meta.get("image_id") or f"{split}_{idx:04d}")

        img_dir = out_root / split / "images"
        ann_dir = out_root / split / "annotations"
        img_dir.mkdir(parents=True, exist_ok=True)
        ann_dir.mkdir(parents=True, exist_ok=True)

        if (split, doc_id) in seen:
            sys.exit(f"duplicate document id {doc_id!r} in split {split!r} — refusing to "
                     f"overwrite. Check the parquet set for a partial or doubled download.")
        seen.add((split, doc_id))

        blob = image_bytes(row["image"])
        img_name = f"{doc_id}{image_ext(blob)}"
        (img_dir / img_name).write_bytes(blob)

        parse = gt.get("gt_parse")
        if not parse:
            parse = rebuild_gt_parse(gt.get("valid_line"))
            rebuilt[split] += 1

        (ann_dir / f"{doc_id}.json").write_text(json.dumps({
            "image_id": doc_id,
            "image_file": f"images/{img_name}",
            "split": split,
            # No language field: CORD's meta does not carry one, and an earlier version of this
            # script wrote "id" unconditionally -- asserting something the source never said.
            # Nothing reads it (the adapter does not), but a fabricated field in a ground-truth
            # file is exactly the kind of thing that later gets cited as evidence.
            "gt_parse": parse,
            "gt_parse_source": "cord" if gt.get("gt_parse") else "rebuilt",
            "valid_line": gt.get("valid_line") or [],
            "meta": meta,
            "roi": gt.get("roi"),
            "repeating_symbol": gt.get("repeating_symbol") or [],
            "dontcare": gt.get("dontcare") or [],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        counts[split] += 1

    if not counts:
        sys.exit("wrote nothing — check --splits and the input route")
    for split in sorted(counts):
        note = f"  ({rebuilt[split]} gt_parse rebuilt from valid_line)" if rebuilt[split] else ""
        print(f"  {split:11s} {counts[split]:4d} documents{note}")
    if skipped:
        print(f"  skipped (not in --splits): {dict(skipped)}")
    if unknown:
        print(f"  !! {sum(unknown.values())} records had no meta.split and no split in their "
              f"path: {dict(unknown)}")

    print(f"\nwrote {sum(counts.values())} documents under {out_root}")
    expected = {"train": 800, "dev": 100, "test": 100}
    bad = [f"{s}: got {counts[s]}, expected {expected[s]}"
           for s in sorted(want & set(expected)) if counts[s] != expected[s]]
    if bad:
        print("\n!! COUNT MISMATCH — CORD-v2 is train 800 / dev 100 / test 100")
        for b in bad:
            print(f"   {b}")
        print("   A short count means a partial download; a long one means duplicated parquet.")
        return 1
    print("counts match CORD-v2: train 800 · dev 100 · test 100")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
