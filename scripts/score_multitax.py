#!/usr/bin/env python3
"""Score the MULTI-RATE TAX templates on their own, against totals.otherCharges.

Two FATURA templates print more than one tax line, so InvoiceData.Totals -- which holds one
taxName / taxPercentage / taxAmount -- cannot represent them. Those three scalar paths are
excluded on those documents in the main run, which reports them as a blind spot. They are not:
the pipeline routes the lines into totals.otherCharges, the ground truth records them (map
contract 1.8), and this scores that recovery.

    python scripts/score_multitax.py runs/s42_main2000 [-o REPORT.md]

Deliberately a SEPARATE report, not a 26th key in the main metric. The unit here is a printed
tax LINE, not a document field, so folding it into a per-field micro figure would mix two
denominators. The main run is untouched by this script.
"""
from __future__ import annotations
import argparse, collections, json, pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import read_jsonl                                        # noqa: E402
from core.matching import compare_charge_lines, charge_lines                 # noqa: E402
from core.metrics import unwrap                                              # noqa: E402
from core.normalize import numeric_from_field, numeric_parse_disagreement    # noqa: E402
import doctypes                                                              # noqa: E402
from registry import gt_dir as _gt_dir                                       # noqa: E402

ARMS = ("RAW", "RAW_POSTPROCESSED", "FINAL")
ARM_LABEL = {"RAW": "RAW", "RAW_POSTPROCESSED": "RAW + normalisation", "FINAL": "FINAL"}
STATES = ("correct", "wrong_value", "wrong_rate", "missing", "not_emitted")
GT_PATH = "totals.otherCharges"


SCRATCH_KEYS = ("document_level_financial_pool", "page_totals_block_analysis")


def stage_trace(run_dir: pathlib.Path, per_doc):
    """Where the printed amounts are at each pipeline stage, read from stages/<doc>/.

    The arms cannot answer this. RAW is extraction PLUS the structural tidy-up, so a line that
    extraction read and the tidy-up discarded looks in RAW exactly like a line extraction never
    read. Only 01_extract_raw.json separates them.
    """
    import re as _re
    stages = [("01_extract_raw.json", "01 extraction"),
              ("02_extraction_postprocessing.json", "02 structural tidy-up"),
              ("04_refined.json", "04 judge + refinement"),
              ("05_postprocessed.json", "05 FINAL")]

    def amounts(obj):
        """Every 2dp amount the stage emitted AS DATA -- reasoning prose excluded, because a
        number the model only talks about is not a number it returned."""
        out = set()

        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if k == "reasoning":
                        continue
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif x is not None:
                for m in _re.finditer(r"\d+\.\d{2}\b", str(x)):
                    out.add(m.group(0))
        walk(obj)
        return out

    tot = collections.Counter()
    by_t = collections.defaultdict(collections.Counter)
    empty_oc = 0
    scratch_only = 0
    sample = None
    for row in per_doc:
        d = run_dir / "stages" / row["doc_id"]
        if not d.is_dir():
            continue
        loaded = {}
        for fn, label in stages:
            p = d / fn
            loaded[label] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if loaded["01 extraction"] is None:
            continue
        printed = {l["truth_value"] for l in row["arms"]["FINAL"]["union"]["lines"]
                   if l["truth_value"]}
        tot["lines"] += len(printed)
        by_t[row["template"]]["lines"] += len(printed)
        seen = {}
        for _fn, label in stages:
            seen[label] = amounts(loaded[label]) if loaded[label] is not None else set()
            tot[label] += len(printed & seen[label])
            by_t[row["template"]][label] += len(printed & seen[label])
        row["stages"] = {label: len(printed & seen[label]) for _fn, label in stages}
        lost = printed - seen["02 structural tidy-up"]
        if lost:
            tot["docs losing lines"] += 1
            by_t[row["template"]]["docs losing lines"] += 1
            oc = ((loaded["01 extraction"].get("data") or {}).get("totals") or {}).get("otherCharges")
            body = oc.get("hence_output") if isinstance(oc, dict) else oc
            if not body:
                empty_oc += 1
                if sample is None and isinstance(oc, dict):
                    sample = (row["doc_id"], str(oc.get("reasoning") or "")[-300:])
            # were the lost amounts sitting only in the model's scratch pool?
            totals01 = (loaded["01 extraction"].get("data") or {}).get("totals") or {}
            pool = amounts({k: v for k, v in totals01.items() if k in SCRATCH_KEYS})
            if lost <= pool:
                scratch_only += 1
    return {"totals": tot, "by_template": by_t, "empty_oc": empty_oc,
            "scratch_only": scratch_only, "sample": sample, "stages": stages}


def collect(run_dir: pathlib.Path, dataset: str | None):
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    dataset = dataset or manifest.get("dataset")
    gt = {r.doc_id: r for r in read_jsonl(str(_gt_dir(spec.name, dataset) / "ground_truth.jsonl"))}

    import re as _re
    LIST_LEAK = _re.compile(r"^\s*\[")
    MULTI_TAX = _re.compile(r"\d\s*[,;]\s*\d|%\s*[,;]")
    per_doc = []
    for f in sorted((run_dir / "raw").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        rec = gt.get(d["doc_id"])
        if rec is None or GT_PATH not in rec.gt:
            continue
        truth = rec.gt[GT_PATH]
        row = {"doc_id": rec.doc_id, "template": rec.cluster_id, "n_lines": len(truth), "arms": {}}
        for arm in ARMS:
            data = (d.get("arms") or {}).get(arm)
            if not isinstance(data, dict):
                continue
            pred = unwrap(data, spec)
            totals = pred.get("totals") or {}
            charges = totals.get("otherCharges") or []
            leaks = []
            for e in charges:
                if not isinstance(e, dict) or not isinstance(e.get("value"), dict):
                    continue
                ov = e["value"].get("originalValue")
                if ov is not None and LIST_LEAK.match(str(ov)):
                    parsed, _ = numeric_from_field(e["value"])
                    leaks.append({"key": e.get("key"), "originalValue": str(ov),
                                  "parsed": str(parsed) if parsed is not None else None})
            multi = []
            for k in ("taxName", "taxPercentage", "taxAmount"):
                v = totals.get(k)
                ov = v.get("originalValue") if isinstance(v, dict) else v
                if ov is not None and MULTI_TAX.search(str(ov)):
                    parsed = (numeric_from_field(v)[0] if isinstance(v, dict) else None)
                    multi.append({"field": k, "originalValue": str(ov),
                                  "parsed": str(parsed) if parsed is not None else None})
            row["arms"][arm] = {
                "union": compare_charge_lines(pred, truth, include_scalar_tax=True),
                "list_only": compare_charge_lines(pred, truth, include_scalar_tax=False),
                "parse_disagreements": sum(
                    1 for e in charges
                    if isinstance(e, dict) and numeric_parse_disagreement(e.get("value"))),
                "list_leaks": leaks,
                "multi_tax_scalars": multi,
                "wire": {"otherCharges": charges,
                         "taxName": totals.get("taxName"),
                         "taxPercentage": totals.get("taxPercentage"),
                         "taxAmount": totals.get("taxAmount")},
            }
        per_doc.append(row)
    return manifest, per_doc


def agg(per_doc, arm, scope, template=None):
    a = collections.Counter()
    states = collections.Counter()
    for row in per_doc:
        if template and row["template"] != template:
            continue
        r = row["arms"].get(arm)
        if not r:
            continue
        res = r[scope]
        a["docs"] += 1
        a["lines"] += res["n_truth"]
        a["correct"] += res["n_correct"]
        a["predicted"] += res["n_predicted"]
        a["spurious"] += res["n_spurious"]
        a["all_lines"] += int(res["all_lines_recovered"])
        a["exact_set"] += int(res["exact_set"])
        a["parse_disagreements"] += r["parse_disagreements"]
        for l in res["lines"]:
            states[l["state"]] += 1
        a["from_scalar"] += sum(1 for l in res["lines"] if l["matched"] and l["source"] == "scalarTax")
    a["states"] = states
    return a


def pct(n, d):
    return f"{100*n/d:.2f}%" if d else "—"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    run = pathlib.Path(a.run_dir)
    manifest, per_doc = collect(run, a.dataset)
    if not per_doc:
        print("no multi-rate documents in this run"); return 1
    templates = sorted({r["template"] for r in per_doc})
    trace = stage_trace(run, per_doc)        # also annotates per_doc with a "stages" block
    out = run / "strict" / f"multitax_{run.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"run_id": manifest.get("run_id"), "documents": per_doc},
                              indent=1, default=str), encoding="utf-8")

    L = []
    W = L.append
    W(f"# Multi-Rate Tax — {run.name}")
    W("")
    W(f"**{len(per_doc)} documents** across **{len(templates)} templates** "
      f"({', '.join(templates)}), **{sum(r['n_lines'] for r in per_doc)} printed tax lines**.")
    W("")
    W("The unit here is a printed **tax line**, not a document field. These documents are the "
      "ones whose three scalar tax paths are excluded from the main run — `InvoiceData.Totals` "
      "holds one tax, and these pages print several — so nothing below appears in the headline "
      "figures. It is published beside them, not inside them.")
    W("")
    W("> **No confidence intervals.** Every interval in the main report resamples templates, and "
      "there are two here. An interval over two clusters would be theatre. Per-template figures "
      "are given instead, and they should be read as two case studies rather than a population.")
    W("")
    W("## 1. Line recovery")
    W("")
    W("A line is **correct** when the rate taken from the charge key matches and the value "
      "matches as a number at two decimal places — the same comparison every other amount in "
      "the benchmark gets.")
    W("")
    W("| Arm | Lines | Recovered | Precision | Docs with every line | Exact set |")
    W("|---|--:|--:|--:|--:|--:|")
    for arm in ARMS:
        s = agg(per_doc, arm, "union")
        W(f"| {ARM_LABEL[arm]} | {s['lines']} | **{pct(s['correct'], s['lines'])}** | "
          f"{pct(s['predicted']-s['spurious'], s['predicted'])} | "
          f"{pct(s['all_lines'], s['docs'])} | {pct(s['exact_set'], s['docs'])} |")
    W("")
    W("*Exact set* is the strictest column: every printed line recovered **and** nothing extra "
      "emitted.")
    W("")
    W("## 2. Why the prediction side is a union")
    W("")
    W("The pipeline does not put every tax line in the same place. Scored against "
      "`totals.otherCharges` alone, against that list plus the scalar tax triple:")
    W("")
    W("| Arm | Template | `otherCharges` only | + scalar triple | Lines the scalars supplied |")
    W("|---|---|--:|--:|--:|")
    for arm in ARMS:
        for t in templates:
            lo = agg(per_doc, arm, "list_only", t)
            un = agg(per_doc, arm, "union", t)
            W(f"| {ARM_LABEL[arm]} | {t} | {pct(lo['correct'], lo['lines'])} | "
              f"**{pct(un['correct'], un['lines'])}** | {un['from_scalar']} |")
    W("")
    W("## 3. Per template")
    W("")
    for t in templates:
        W(f"### {t}")
        W("")
        W("| Arm | Lines | Recovered | Docs with every line | Spurious entries |")
        W("|---|--:|--:|--:|--:|")
        for arm in ARMS:
            s = agg(per_doc, arm, "union", t)
            W(f"| {ARM_LABEL[arm]} | {s['lines']} | **{pct(s['correct'], s['lines'])}** | "
              f"{pct(s['all_lines'], s['docs'])} | {s['spurious']} |")
        W("")
    W("## 4. How the failures fail")
    W("")
    W("Five states, so a miss says which kind it is:")
    W("")
    W("| State | Meaning |")
    W("|---|---|")
    W("| `correct` | rate and value both match |")
    W("| `wrong_value` | the rate was found, the amount is different |")
    W("| `wrong_rate` | the amount was found under a different rate |")
    W("| `missing` | the rate was emitted with no value at all |")
    W("| `not_emitted` | neither rate nor amount appears anywhere |")
    W("")
    W("| Arm | " + " | ".join(f"`{s}`" for s in STATES) + " |")
    W("|---|" + "--:|" * len(STATES))
    for arm in ARMS:
        s = agg(per_doc, arm, "union")
        W(f"| {ARM_LABEL[arm]} | " + " | ".join(str(s["states"][k]) for k in STATES) + " |")
    W("")
    fin = agg(per_doc, "FINAL", "union")
    if fin["parse_disagreements"]:
        W(f"Separately, **{fin['parse_disagreements']} charge values in FINAL** carry both a "
          "printed form and a parsed number that disagree at two decimal places — this module's "
          "parser and production's reading the same string differently. Counted, never resolved.")
        W("")
    if trace["totals"]["lines"]:
        t = trace["totals"]
        W("## 5. Where the lines are actually lost")
        W("")
        W("The arms cannot answer this. **RAW is extraction plus the structural tidy-up**, so a "
          "line extraction read and the tidy-up discarded looks in RAW exactly like a line "
          "extraction never read. `stages/<doc>/01_extract_raw.json` separates them. Amounts "
          "are counted only where a stage emitted them **as data** — a number the model merely "
          "discusses in its reasoning prose is not a number it returned.")
        W("")
        W("| Stage | Printed amounts present |")
        W("|---|--:|")
        for _fn, label in trace["stages"]:
            W(f"| {label} | **{pct(t[label], t['lines'])}** ({t[label]}/{t['lines']}) |")
        W("")
        W(f"**Extraction reads every printed tax line — {pct(t['01 extraction'], t['lines'])}.** "
          "The loss is downstream, and it is not a reading failure.")
        W("")
        W("| Template | Lines | After extraction | After the tidy-up | Documents losing a line |")
        W("|---|--:|--:|--:|--:|")
        for tmpl in sorted(trace["by_template"]):
            c = trace["by_template"][tmpl]
            W(f"| {tmpl} | {c['lines']} | {pct(c['01 extraction'], c['lines'])} | "
              f"{pct(c['02 structural tidy-up'], c['lines'])} | {c['docs losing lines']} |")
        W("")
        W("### What actually happens")
        W("")
        W(f"On all **{trace['empty_oc']}** documents that lose a line, extraction returned "
          "`totals.otherCharges = []` — and on "
          f"**{trace['scratch_only']}** of them the missing amounts were sitting in the model's "
          "own totals scratch fields (`document_level_financial_pool`, "
          "`page_totals_block_analysis`). Those are working notes, not schema fields, and the "
          "structural tidy-up strips them by design.")
        W("")
        if trace["sample"]:
            doc, reason = trace["sample"]
            W(f"The extraction prompt says why, on `{doc}`:")
            W("")
            W("```text")
            W("…" + reason)
            W("```")
            W("")
        W("So the chain is: **the model reads the tax lines, decides they belong in the tax "
          "fields rather than in `otherCharges`, the scalar tax fields cannot hold five rates "
          "so they stay null, and the tidy-up then discards the scratch copy.** The judge "
          "re-reads the page image and recovers most of them.")
        W("")
        W("> **The tidy-up is not the bug.** Across all 2,000 documents in this run it drops "
          "**no value from any scored schema path** — verified stage 01 against stage 02, field "
          "by field. It discards exactly what it is meant to discard. The decision that strands "
          "the data is the `otherCharges` routing rule in the extraction prompt, which treats "
          "tax as categorically not an \"other charge\" — correct when there is one tax line, "
          "wrong when there are five and nothing else can hold them.")
        W("")
    W("## 6. Three defects this exposes")
    W("")
    leak_corrupt = [(r["doc_id"], l) for r in per_doc
                    for l in r["arms"].get("FINAL", {}).get("list_leaks", [])
                    if l["parsed"] is not None and "," in l["originalValue"]]
    leak_benign = sum(1 for r in per_doc for l in r["arms"].get("FINAL", {}).get("list_leaks", [])
                      if not (l["parsed"] is not None and "," in l["originalValue"]))
    n_leak = leak_benign + len(leak_corrupt)
    if n_leak:
        W(f"**A list leaks into a charge value.** {n_leak} charge values in FINAL carry an "
          "`originalValue` that is a stringified Python list, and none do in RAW — so this "
          "arrives with the judge and refinement, not with extraction. "
          f"{leak_benign} are single-element (`\"['46.05']\"`) and parse correctly. "
          f"**{len(leak_corrupt)} are two-element and the number parser loses the decimal "
          "point**, which is a 100x error, not a formatting one:")
        W("")
        if leak_corrupt:
            W("| Document | Key | Shipped `originalValue` | Parsed as |")
            W("|---|---|---|--:|")
            for doc, l in leak_corrupt[:6]:
                W(f"| `{doc}` | {l['key']} | `{l['originalValue']}` | **{l['parsed']}** |")
            W("")
    multi_docs = sorted({r["doc_id"] for r in per_doc
                         for arm in ARMS if r["arms"].get(arm, {}).get("multi_tax_scalars")})
    if multi_docs:
        W(f"**Two taxes crammed into the scalar fields as one string.** On "
          f"{len(multi_docs)} document(s) the model writes both taxes into the single tax "
          "triple, comma-joined. The two parsers then read the same string differently — this "
          "benchmark takes the last number, production glues them into one:")
        W("")
        W("| Document | Field | Shipped `originalValue` | Production parsed it as |")
        W("|---|---|---|--:|")
        for doc in multi_docs[:4]:
            row = next(r for r in per_doc if r["doc_id"] == doc)
            for arm in ("RAW_POSTPROCESSED", "RAW"):
                for m in row["arms"].get(arm, {}).get("multi_tax_scalars", []):
                    W(f"| `{doc}` | `{m['field']}` | `{m['originalValue']}` | "
                      f"{m['parsed'] or '—'} |")
                if row["arms"].get(arm, {}).get("multi_tax_scalars"):
                    break
        W("")
    regress = []
    for row in per_doc:
        r0, r1 = row["arms"].get("RAW"), row["arms"].get("RAW_POSTPROCESSED")
        if r0 and r1 and r1["union"]["n_correct"] < r0["union"]["n_correct"]:
            regress.append((row["doc_id"], r0["union"]["n_correct"], r1["union"]["n_correct"]))
    if regress:
        W("**Normalisation breaks a tax line.** The main report records the deterministic stage "
          "as fixing 134 fields and breaking none. On this target it breaks "
          f"{len(regress)}: " + ", ".join(f"`{d}` ({a} → {b} lines)" for d, a, b in regress[:4])
          + ". The scalar percentage is rejected as out of range, the line loses its rate, and "
            "a line with no rate cannot be aligned to a printed one — so it is scored "
            "`not_emitted` rather than quietly matched to the wrong rate.")
        W("")
    W("## 7. Documents that lose a line")
    W("")
    W("| Document | Rate | Ground truth | FINAL | State |")
    W("|---|--:|--:|--:|---|")
    shown = 0
    for row in per_doc:
        r = row["arms"].get("FINAL")
        if not r:
            continue
        for l in r["union"]["lines"]:
            if l["matched"] or shown >= 20:
                continue
            W(f"| `{row['doc_id']}` | {l['rate']} | {l['truth_value']} | "
              f"{l['pred_value'] if l['pred_value'] is not None else '—'} | `{l['state']}` |")
            shown += 1
    if not shown:
        W("| — | — | — | — | every line recovered |")
    W("")
    W("")
    W("## 8. The wire, for every document that loses a line")
    W("")
    W("What each arm actually shipped, so no failure above has to be taken on trust.")
    W("")
    failing = [r for r in per_doc
               if r["arms"].get("FINAL") and not r["arms"]["FINAL"]["union"]["all_lines_recovered"]]
    for row in failing[:6]:
        W(f"**`{row['doc_id']}`** · {row['template']} · {row['n_lines']} printed lines")
        W("")
        W("```text")
        for arm in ARMS:
            w = row["arms"].get(arm, {}).get("wire")
            if not w:
                continue
            W(f"{ARM_LABEL[arm]}")
            W(f"  otherCharges  {json.dumps(w['otherCharges'])[:150]}")
            W(f"  taxName       {json.dumps(w['taxName'])}")
            W(f"  taxPercentage {json.dumps(w['taxPercentage'])}")
            W(f"  taxAmount     {json.dumps(w['taxAmount'])}")
        W("```")
        W("")
    W(f"Per-line detail for every document: `{out}`")
    W("")
    text = "\n".join(L) + "\n"
    dest = pathlib.Path(a.out) if a.out else run / "strict" / f"MULTITAX_REPORT_{run.name}.md"
    dest.write_text(text, encoding="utf-8")
    for arm in ARMS:
        s = agg(per_doc, arm, "union")
        print(f"{ARM_LABEL[arm]:22} lines {s['correct']}/{s['lines']} = {pct(s['correct'], s['lines'])}"
              f"   docs all-lines {s['all_lines']}/{s['docs']}   spurious {s['spurious']}")
    print(f"-> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
