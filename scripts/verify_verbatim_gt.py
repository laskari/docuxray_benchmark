"""Gate for gt/invoice/fatura_verbatim: every stored value must be text FROM the source.

The whole claim of a verbatim ground truth is that no value in it was invented, reformatted
or re-ordered. That claim is checkable, so it is checked rather than asserted: each stored
value must appear as a literal substring of the annotation it came from.

Three documented exemptions, all derived rather than transcribed:
  * `currency`, `*ISO` dates, and the GST rate live in a label or are computed -- no source
    span exists for them, so the verbatim policy does not apply and they are inherited.
  * an ALL_CAPS annotator prefix ('BALANCE_DUE : 481.84 $') is stripped, so the check runs
    against the stripped source.
"""
from __future__ import annotations
import json, pathlib, sys
from collections import Counter

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from core.canonical import read_jsonl                                    # noqa: E402
from core.normalize import _Absent, strip_label_prefix                   # noqa: E402
from config import load as load_cfg                                      # noqa: E402

ANN = pathlib.Path(load_cfg()["paths"]["dataset"]).expanduser()
if not ANN.is_absolute():
    ANN = (_ROOT / ANN).resolve()
ANN = ANN / "modified_annotations"

# path -> the annotation labels that can write it. Only reverted paths are checked.
SOURCES = {
    "parties.customer.phone":            [("BUYER", "Tel"), ("BILL_TO", "Tel")],
    "parties.shipTo.phone":              [("SEND_TO", "Tel")],
    "parties.seller.addressStructured":  [("SELLER", "Address")],
    "parties.customer.addressStructured":[("BUYER", "Address"), ("BILL_TO", "Address")],
    "parties.shipTo.addressStructured":  [("SEND_TO", "Address")],
    "totals.totalIncludingTax":          [("TOTAL", None), ("AMOUNT_DUE", None)],
    "totals.subtotal":                   [("SUB_TOTAL", None)],
    "totals.taxAmount":                  [("TAX", None)],
    "totals.taxPercentage":              [("TAX", None)],
    "totals.discountTotal":              [("DISCOUNT", None)],
    "totals.discountPercentage":         [("DISCOUNT", None)],
}
# GST(n%) labels also write taxAmount / taxPercentage; matched by prefix at lookup time.
EXEMPT_PREFIX = "GST("


def sources_for(ann: dict, path: str):
    out = []
    for label, sub in SOURCES.get(path, []):
        v = ann.get(label)
        if sub:
            v = v.get(sub) if isinstance(v, dict) else None
        if isinstance(v, str) and v.strip():
            out.append(v)
    if path.startswith("totals.tax"):
        out += [v for k, v in ann.items()
                if k.startswith(EXEMPT_PREFIX) and isinstance(v, str) and v.strip()]
    return out


def main() -> int:
    gt = read_jsonl(str(_ROOT / "gt" / "invoice" / "fatura_verbatim" / "ground_truth.jsonl"))
    checked = Counter(); exempt = Counter(); bad = []
    for rec in gt:
        apath = ANN / f"{rec.doc_id}.json"
        if not apath.exists():
            continue
        ann = json.loads(apath.read_text(encoding="utf-8"))
        for path, value in rec.gt.items():
            if isinstance(value, _Absent) or path not in SOURCES:
                continue
            srcs = sources_for(ann, path)
            if not srcs:
                continue
            if path == "totals.taxPercentage" and "TAX" not in ann and any(
                    k.startswith(EXEMPT_PREFIX) for k in ann):
                exempt[path] += 1          # rate lives in the GST(n%) key -- group D
                continue
            checked[path] += 1
            if not any(str(value) in strip_label_prefix(s) for s in srcs):
                bad.append((rec.doc_id, path, value, srcs))

    total = sum(checked.values())
    print(f"checked {total:,} stored values across {len(checked)} reverted paths")
    for p in sorted(checked):
        print(f"  {p:38s} {checked[p]:6d}")
    if exempt:
        print("\nderived, exempt by policy (no source span exists):")
        for p in sorted(exempt):
            print(f"  {p:38s} {exempt[p]:6d}   rate lives in the GST(n%) label")
    if bad:
        print(f"\nFAIL: {len(bad)} values are not substrings of their source")
        for b in bad[:15]:
            print("   ", b[0], b[1], repr(b[2]), "not in", [repr(x) for x in b[3]])
        return 1
    print("\nPASS: every stored value is a literal substring of its source annotation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
