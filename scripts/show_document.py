"""Print every layer of one FATURA document, so a ground-truth value can be checked by eye.

    python3 scripts/show_document.py Template10_Instance10
    python3 scripts/show_document.py Template10_Instance10 --run runs/s42_main1000

Layers, in provenance order:

  1  images/<doc>.jpg                      the page itself -- open it beside this output
  2  Annotations/<doc>.json                FATURA as published: bbox + the printed `text`
  3  modified_annotations/<doc>.json       flat key/value. DERIVED, not raw: fatura_to_keyvalue.py
                                           strips the in-document label ('TOTAL : 408.61 USD' ->
                                           '408.61 USD') and collapses runs of 2+ spaces
  4  gt/invoice/fatura/                    the benchmark's canonical ground truth (parsed)
  5  gt/invoice/fatura_verbatim/           the verbatim ground truth (printed span kept)
  6  runs/<id>/raw/<doc>.json              what each arm shipped, if --run is given

Nothing here is computed: every line is read from a file on disk and the path is printed above
it, so any value in the report can be traced back to the page in one command.
"""
from __future__ import annotations
import argparse, json, pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.canonical import read_jsonl                                    # noqa: E402
from core.normalize import _Absent                                       # noqa: E402
from config import load as load_cfg                                      # noqa: E402
import core.metrics as M, doctypes                                       # noqa: E402


def dataset_root() -> pathlib.Path:
    p = pathlib.Path(load_cfg()["paths"]["dataset"]).expanduser()
    return p if p.is_absolute() else (_ROOT / p).resolve()


def head(t):
    print("\n" + "=" * 78 + f"\n{t}\n" + "=" * 78)


def gt_row(subdir, doc):
    f = _ROOT / "gt" / "invoice" / subdir / "ground_truth.jsonl"
    if not f.exists():
        return None, f
    for line in open(f, encoding="utf-8"):
        if f'"doc_id": "{doc}"' in line or f'"doc_id":"{doc}"' in line:
            from core.canonical import BenchmarkRecord
            return BenchmarkRecord.from_json(line), f
    return None, f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc_id")
    ap.add_argument("--run", default=None)
    a = ap.parse_args()
    doc, root = a.doc_id, dataset_root()

    print(f"document : {doc}")
    print(f"page     : {root / 'images' / (doc + '.jpg')}")

    head("2. SOURCE, untouched  --  Annotations/  (bbox omitted)")
    p = root / "Annotations" / f"{doc}.json"
    print(f"  {p}\n")
    try:
        orig = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        for k, v in orig.items():
            if isinstance(v, dict) and "text" in v:
                print(f"  {k:16s} {v['text']!r}")
            elif k not in ("TABLE", "INVOICE_INFO"):
                print(f"  {k:16s} {v!r}"[:200])
    except Exception as e:                                              # noqa: BLE001
        print(f"  (unreadable: {e})")

    head("3. DERIVED key/value  --  modified_annotations/  (what the adapter reads)")
    p = root / "modified_annotations" / f"{doc}.json"
    print(f"  {p}")
    print("  NOTE: label prefix stripped and runs of 2+ spaces collapsed by "
          "fatura_to_keyvalue.py\n")
    mod = json.loads(p.read_text(encoding="utf-8"))
    print(json.dumps(mod, indent=2, ensure_ascii=False))

    can, canf = gt_row("fatura", doc)
    vrb, vrbf = gt_row("fatura_verbatim", doc)
    head("4/5. GROUND TRUTH  --  canonical vs verbatim")
    print(f"  canonical : {canf}")
    print(f"  verbatim  : {vrbf}\n")
    fmt = lambda v: "ABSENT" if isinstance(v, _Absent) else repr(v)
    paths = sorted(set(can.gt if can else {}) | set(vrb.gt if vrb else {}))
    w = max((len(p) for p in paths), default=10)
    print(f"  {'path'.ljust(w)}  {'canonical':<34} verbatim")
    print(f"  {'-'*w}  {'-'*34} {'-'*34}")
    for path in paths:
        c = fmt(can.gt.get(path)) if can else "-"
        v = fmt(vrb.gt.get(path)) if vrb else "-"
        mark = "   " if c == v else " * "
        print(f"  {path.ljust(w)}{mark}{c:<34} {v}")
    print("\n  '*' marks a path where the two ground truths differ.")

    if a.run:
        head("6. WHAT THE PIPELINE SHIPPED")
        f = _ROOT / a.run / "raw" / f"{doc}.json"
        print(f"  {f}\n")
        d = json.loads(f.read_text(encoding="utf-8"))
        spec = doctypes.get("invoice")
        flats = {arm: M.flatten_prediction(M.unwrap(v, spec))
                 for arm, v in (d.get("arms") or {}).items() if isinstance(v, dict)}
        arms = [x for x in ("RAW", "RAW_POSTPROCESSED", "FINAL") if x in flats]
        print(f"  {'path'.ljust(w)}  " + "  ".join(f"{x:<26}" for x in arms))
        print(f"  {'-'*w}  " + "  ".join("-" * 26 for _ in arms))
        for path in paths:
            cells = []
            for arm in arms:
                v = flats[arm].get(path)
                if isinstance(v, dict) and set(v) <= {"originalValue", "normalizedValue"}:
                    v = f"{v.get('originalValue')!r}/{v.get('normalizedValue')!r}"
                cells.append(str(v)[:26])
            print(f"  {path.ljust(w)}  " + "  ".join(f"{c:<26}" for c in cells))
        print("\n  numerics show originalValue/normalizedValue.")


if __name__ == "__main__":
    main()
