"""What would a VERBATIM FATURA ground truth recover? Measured, not assumed.

For each scored key, build the verbatim value from the raw FATURA annotation (the printed
span, no parsing) and byte-compare it against what each arm ships.
"""
import json, os, re, sys, glob
from collections import Counter, defaultdict
sys.path.insert(0, '.')
from core.canonical import read_jsonl
from core.normalize import _Absent, strip_label_prefix

ANN = os.path.expanduser("../../Benchmark/FATURA_invoices_dataset/modified_annotations")
RUN = "runs/s42_main1000"
gt = {r.doc_id: r for r in read_jsonl("gt/invoice/fatura/ground_truth.jsonl")}

def U(d): return d.get("invoiceOutputData", d)
def dig(o, path):
    cur = o
    for part in path.split("."):
        if not isinstance(cur, dict): return None
        cur = cur.get(part)
    return cur

def shipped(o, path, key):
    v = dig(o, path)
    if isinstance(v, dict):
        if key == "normalizedValue":
            raw = v.get("normalizedValue")
            if raw is None or isinstance(raw, bool): return None
            return str(raw) if isinstance(raw, int) else repr(raw)
        return v.get("originalValue")
    return v

def paren_pct(s):
    m = re.search(r"\(\s*(\d+(?:\.\d+)?\s*%)\s*\)", s)
    return m.group(1).strip() if m else None

def after_colon(s):
    s = strip_label_prefix(str(s))
    return s.split(":", 1)[1].strip() if ":" in s else s.strip()

def whole(s):
    return strip_label_prefix(str(s)).strip()

# key -> (label candidates, span function)
SPEC = {
    "parties.customer.phone":      (["BUYER.Tel", "BILL_TO.Tel"], whole),
    "parties.shipTo.phone":        (["SEND_TO.Tel"], whole),
    "totals.totalIncludingTax":    (["TOTAL", "AMOUNT_DUE"], whole),
    "totals.subtotal":             (["SUB_TOTAL"], whole),
    "totals.taxAmount":            (["TAX"], after_colon),
    "totals.taxPercentage":        (["TAX"], paren_pct),
    "totals.discountTotal":        (["DISCOUNT"], after_colon),
    "totals.discountPercentage":   (["DISCOUNT"], paren_pct),
}
NUMERIC = {k for k in SPEC if k.startswith("totals.")}

def label_value(ann, name):
    if "." in name:
        a, b = name.split(".", 1)
        blk = ann.get(a)
        return blk.get(b) if isinstance(blk, dict) else None
    return ann.get(name)

stat = defaultdict(Counter)
ex = defaultdict(list)
for f in sorted(glob.glob(f"{RUN}/raw/*.json")):
    d = json.load(open(f, encoding="utf-8"))
    if not d.get("ok"): continue
    doc = d["doc_id"]
    rec = gt.get(doc)
    if rec is None: continue
    apath = os.path.join(ANN, doc + ".json")
    if not os.path.exists(apath): continue
    ann = json.load(open(apath, encoding="utf-8"))
    for key, (labels, span) in SPEC.items():
        if key not in rec.annotated_fields: continue
        truth = rec.gt.get(key)
        if truth is None or isinstance(truth, _Absent): continue
        src = next((label_value(ann, L) for L in labels if label_value(ann, L)), None)
        if src is None: continue
        vb = span(src)
        if not vb: continue
        for arm in ("RAW", "RAW_POSTPROCESSED", "FINAL"):
            o = U(d["arms"][arm])
            k = "originalValue" if key in NUMERIC else None
            got = shipped(o, key, "originalValue")
            stat[(key, arm)]["n"] += 1
            stat[(key, arm)]["verbatim_hit"] += (got == vb)
            stat[(key, arm)]["current_gt_hit"] += (got == truth)
            if arm == "RAW" and got != vb and len(ex[key]) < 3:
                ex[key].append((doc, got, vb, truth))

print(f"{'key':30s} {'arm':18s} {'n':>5s} {'vs VERBATIM GT':>15s} {'vs CURRENT GT':>14s}")
for key in SPEC:
    for arm in ("RAW", "RAW_POSTPROCESSED", "FINAL"):
        s = stat[(key, arm)]
        if not s["n"]: continue
        print(f"{key:30s} {arm:18s} {s['n']:5d} {s['verbatim_hit']/s['n']*100:14.1f}% "
              f"{s['current_gt_hit']/s['n']*100:13.1f}%")
    print()
print("misses (RAW): doc | shipped originalValue | verbatim span | current GT")
for k, v in ex.items():
    for e in v[:2]: print("  ", k, "|", repr(e[1]), "|", repr(e[2]), "|", repr(e[3]))
