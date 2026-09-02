#!/usr/bin/env python3
"""Export a run to Excel for key-level and file-level review.

One row per (document, field) with ground truth beside every arm, the harness's own verdict,
what changed between arms, and what the judge said about it — plus a blank adjudication column
for the pilot's hand review.

    python scripts/export_review.py runs/smoke [-o review.xlsx]
"""
from __future__ import annotations
import argparse, json, pathlib, sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import read_jsonl                                          # noqa: E402
from core.matching import load_rule_registry, compare                        # noqa: E402
from core.metrics import flatten_prediction, unwrap, predicted_value         # noqa: E402
import doctypes                                                              # noqa: E402
from core.normalize import _Absent                                             # noqa: E402

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

FONT = "Arial"
HDR = PatternFill("solid", fgColor="1F3864")
HDRF = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY = Font(name=FONT, size=10)
MONO = Font(name="Consolas", size=9)
BAD = PatternFill("solid", fgColor="FCE4D6")     # mismatch
GOOD = PatternFill("solid", fgColor="E2EFDA")    # fixed by the judge
HARM = PatternFill("solid", fgColor="F8CBAD")    # broken by the judge
INPUT = PatternFill("solid", fgColor="FFFF00")   # yours to fill in
ABSENT_TXT = "«GT says absent»"
FORMULA_ROW_LIMIT = 5000   # above this, summaries are computed in Python (LibreOffice chokes)


def render(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, _Absent):
        return ABSENT_TXT
    if isinstance(v, dict):
        if set(v) <= {"originalValue", "normalizedValue"}:
            return f"{v.get('originalValue')!r} / {v.get('normalizedValue')}"
        return json.dumps({k: x for k, x in v.items() if x is not None}, ensure_ascii=False)[:200]
    return str(v)[:300]


def judge_lookup(judge: dict | None, path: str):
    if not judge:
        return None
    leaf = path.split(".")[-1]
    for section, issues in (judge.get("issues") or {}).items():
        for i in issues:
            f = str(i.get("field") or "")
            if f and (f == path or f.endswith(leaf) or leaf in f):
                return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    run = pathlib.Path(a.run_dir)
    out = pathlib.Path(a.out) if a.out else run / f"review_{run.name}.xlsx"

    manifest = json.loads((run / "manifest.json").read_text())
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    gt_dir = _ROOT / "gt" / spec.name
    gt = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    cov = {c["path"]: c for c in json.loads((gt_dir / "coverage.json").read_text())["paths"]}

    rows = []
    for f in sorted((run / "raw").glob("*.json")):
        d = json.loads(f.read_text())
        if not d.get("ok"):
            continue
        rec = gt.get(d["doc_id"])
        if rec is None:
            continue
        flats = {arm: flatten_prediction(unwrap(data, spec))
                 for arm, data in (d.get("arms") or {}).items()
                 if not arm.startswith("_") and isinstance(data, dict)}
        for path in sorted(rec.annotated_fields):
            rule = rules.get(path)
            if rule is None:
                continue
            truth = rec.gt.get(path)
            gt_absent = isinstance(truth, _Absent)
            cells, verdicts = {}, {}
            for arm in ("A", "B", "C"):
                pred = predicted_value(path, flats[arm], spec) if arm in flats else None
                cells[arm] = pred
                if arm not in flats:
                    verdicts[arm] = ""
                elif gt_absent:
                    emitted = pred is not None and str(pred).strip() != ""
                    verdicts[arm] = "HALLUCINATION" if emitted else "correct-null"
                else:
                    verdicts[arm] = "match" if compare(pred, truth, rule).matched else "MISMATCH"
            delta = ""
            if verdicts.get("B") and verdicts.get("C"):
                okb, okc = verdicts["B"] in ("match", "correct-null"), verdicts["C"] in ("match", "correct-null")
                delta = "FIXED" if (not okb and okc) else ("HARMED" if (okb and not okc) else "")
            issue = judge_lookup(d.get("judge"), path)
            rows.append({
                "doc_id": d["doc_id"], "template": rec.cluster_id, "field": path,
                "rule": rule.value,
                "headline": "yes" if cov.get(path, {}).get("headline_eligible") else "no",
                "gt": render(truth),
                "A": render(cells["A"]), "B": render(cells["B"]), "C": render(cells["C"]),
                "vA": verdicts["A"], "vB": verdicts["B"], "vC": verdicts["C"],
                "delta": delta,
                "judge_flag": "yes" if issue else "",
                "judge_type": (issue or {}).get("issue_type", ""),
                "judge_corrected": render((issue or {}).get("corrected_value")),
            })

    wb = Workbook()
    _fields_sheet(wb, rows, manifest)
    use_formulas = len(rows) <= FORMULA_ROW_LIMIT
    _by_file(wb, rows, use_formulas)
    _by_field(wb, rows, use_formulas)
    _readme(wb, rows, manifest, use_formulas)
    wb.save(out)
    print(f"{len(rows)} field rows · {len({r['doc_id'] for r in rows})} documents "
          f"· {len({r['field'] for r in rows})} fields")
    print(f"summaries use {'live formulas' if use_formulas else 'computed values (too many rows for formulas)'}")
    print(f"-> {out}")
    return 0


HEADERS = ["doc_id", "template", "field", "rule", "headline",
           "GROUND TRUTH", "A · raw extraction", "B · postprocessed", "C · refined (judge)",
           "A?", "B?", "C?", "B→C", "judge flagged", "judge issue", "judge corrected value",
           "ADJUDICATION", "NOTES"]


def _fields_sheet(wb, rows, manifest):
    ws = wb.active; ws.title = "Fields"
    ws.append(HEADERS)
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 30
    for r in rows:
        ws.append([r["doc_id"], r["template"], r["field"], r["rule"], r["headline"],
                   r["gt"], r["A"], r["B"], r["C"],
                   r["vA"], r["vB"], r["vC"], r["delta"],
                   r["judge_flag"], r["judge_type"], r["judge_corrected"], "", ""])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = BODY
        for i in (2, 5, 6, 7, 8, 15):
            row[i].font = MONO
        for i, key in ((9, "vA"), (10, "vB"), (11, "vC")):
            if row[i].value in ("MISMATCH", "HALLUCINATION"):
                row[i].fill = BAD
                row[i].font = Font(name=FONT, size=10, bold=True, color="C00000")
        if row[12].value == "FIXED":
            row[12].fill = GOOD; row[12].font = Font(name=FONT, size=10, bold=True, color="375623")
        elif row[12].value == "HARMED":
            row[12].fill = HARM; row[12].font = Font(name=FONT, size=10, bold=True, color="C00000")
        row[16].fill = INPUT; row[17].fill = INPUT
    widths = [22, 12, 40, 14, 9, 34, 34, 34, 34, 8, 8, 8, 8, 9, 22, 26, 20, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "F2"
    ws.auto_filter.ref = f"A1:R{ws.max_row}"
    dv = DataValidation(type="list", allow_blank=True, showDropDown=False,
                        formula1='"model_error,gt_error,normalisation_error,mapping_error,not_an_error"')
    ws.add_data_validation(dv); dv.add(f"Q2:Q{ws.max_row}")


def _rate(ws, row, col_letter, n_rows, match_col, label_row):
    return (f'=IFERROR(COUNTIFS(Fields!${col_letter}$2:${col_letter}${n_rows},"match",'
            f'Fields!$A$2:$A${n_rows},$A{label_row})/'
            f'COUNTIFS(Fields!$A$2:$A${n_rows},$A{label_row}),"")')


def _by_file(wb, rows, formulas):
    ws = wb.create_sheet("By file")
    ws.append(["doc_id", "template", "fields", "A correct", "B correct", "C correct",
               "A rate", "B rate", "C rate", "fixed B→C", "harmed B→C"])
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
    n = len(rows) + 1
    docs = sorted({r["doc_id"] for r in rows})
    for i, doc in enumerate(docs, start=2):
        sub = [r for r in rows if r["doc_id"] == doc]
        if formulas:
            ws.append([doc, sub[0]["template"],
                       f'=COUNTIF(Fields!$A$2:$A${n},$A{i})',
                       f'=COUNTIFS(Fields!$A$2:$A${n},$A{i},Fields!$J$2:$J${n},"match")',
                       f'=COUNTIFS(Fields!$A$2:$A${n},$A{i},Fields!$K$2:$K${n},"match")',
                       f'=COUNTIFS(Fields!$A$2:$A${n},$A{i},Fields!$L$2:$L${n},"match")',
                       f"=IFERROR(D{i}/$C{i},\"\")", f"=IFERROR(E{i}/$C{i},\"\")",
                       f"=IFERROR(F{i}/$C{i},\"\")",
                       f'=COUNTIFS(Fields!$A$2:$A${n},$A{i},Fields!$M$2:$M${n},"FIXED")',
                       f'=COUNTIFS(Fields!$A$2:$A${n},$A{i},Fields!$M$2:$M${n},"HARMED")'])
        else:
            t = len(sub)
            ca, cb, cc = (sum(1 for r in sub if r[k] == "match") for k in ("vA", "vB", "vC"))
            ws.append([doc, sub[0]["template"], t, ca, cb, cc,
                       ca / t if t else "", cb / t if t else "", cc / t if t else "",
                       sum(1 for r in sub if r["delta"] == "FIXED"),
                       sum(1 for r in sub if r["delta"] == "HARMED")])
    _finish(ws, [24, 12, 8, 10, 10, 10, 9, 9, 9, 11, 12], pct_cols="GHI")


def _by_field(wb, rows, formulas):
    ws = wb.create_sheet("By field")
    ws.append(["field", "rule", "headline", "n", "A correct", "B correct", "C correct",
               "A rate", "B rate", "C rate", "hallucinations (C)"])
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
    n = len(rows) + 1
    fields = sorted({r["field"] for r in rows})
    for i, fld in enumerate(fields, start=2):
        sub = [r for r in rows if r["field"] == fld]
        if formulas:
            ws.append([fld, sub[0]["rule"], sub[0]["headline"],
                       f'=COUNTIF(Fields!$C$2:$C${n},$A{i})',
                       f'=COUNTIFS(Fields!$C$2:$C${n},$A{i},Fields!$J$2:$J${n},"match")',
                       f'=COUNTIFS(Fields!$C$2:$C${n},$A{i},Fields!$K$2:$K${n},"match")',
                       f'=COUNTIFS(Fields!$C$2:$C${n},$A{i},Fields!$L$2:$L${n},"match")',
                       f"=IFERROR(E{i}/$D{i},\"\")", f"=IFERROR(F{i}/$D{i},\"\")",
                       f"=IFERROR(G{i}/$D{i},\"\")",
                       f'=COUNTIFS(Fields!$C$2:$C${n},$A{i},Fields!$L$2:$L${n},"HALLUCINATION")'])
        else:
            t = len(sub)
            ca, cb, cc = (sum(1 for r in sub if r[k] == "match") for k in ("vA", "vB", "vC"))
            ws.append([fld, sub[0]["rule"], sub[0]["headline"], t, ca, cb, cc,
                       ca / t if t else "", cb / t if t else "", cc / t if t else "",
                       sum(1 for r in sub if r["vC"] == "HALLUCINATION")])
    _finish(ws, [42, 14, 9, 7, 10, 10, 10, 9, 9, 9, 17], pct_cols="HIJ", mono_first=True)


def _finish(ws, widths, pct_cols="", mono_first=False):
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = BODY
        if mono_first:
            row[0].font = MONO
        for col in pct_cols:
            ws[f"{col}{row[0].row}"].number_format = "0.0%"
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"


def _readme(wb, rows, manifest, formulas):
    ws = wb.create_sheet("Read me", 0)
    ws.sheet_view.showGridLines = False
    lines = [
        ("Benchmark review — %s" % manifest.get("run_id"), 15, True),
        ("", 10, False),
        (f"{len(rows)} field rows · {len({r['doc_id'] for r in rows})} documents · "
         f"field map {manifest.get('field_map_version')} · "
         f"models {manifest.get('models', {}).get('extraction')} / "
         f"{manifest.get('models', {}).get('judge')}", 10, False),
        ("", 10, False),
        ("The three arms", 12, True),
        ("A · raw extraction — straight from Gemini, 4 split-part calls.", 10, False),
        ("B · postprocessed — A plus extraction_postprocessing and invoice_postprocessor. "
         "NO model call, so B is free; any A→B difference is your deterministic rules.", 10, False),
        ("C · refined — B plus the judge (5 sectional calls) and refinement. The judge is ~74% "
         "of the bill, so the B→C column is what decides whether it earns its cost.", 10, False),
        ("", 10, False),
        ("Columns you fill in (yellow)", 12, True),
        ("ADJUDICATION — for each MISMATCH, which of these it actually is:", 10, False),
        ("    model_error          the model got it wrong", 10, False),
        ("    gt_error             the ground truth is wrong (this dataset has known cases)", 10, False),
        ("    normalisation_error  both are right, the comparison rule is too strict", 10, False),
        ("    mapping_error        the field map points at the wrong schema path", 10, False),
        ("    not_an_error         anything else — explain in NOTES", 10, False),
        ("The share that comes back gt_error is the published ground-truth error rate. It is a "
         "real result, not bookkeeping.", 10, False),
        ("", 10, False),
        ("How to read it", 12, True),
        ("Fields   — one row per (document, field). Filter B→C on HARMED to see every field the "
         "judge broke; on FIXED to see every one it repaired.", 10, False),
        ("By file  — per-document rates. Find the documents that fail across many fields; those "
         "are usually one layout problem, not many field problems.", 10, False),
        ("By field — per-field rates across all documents. 'headline=no' fields are barred from "
         "published claims (too few distinct values, or template constants).", 10, False),
        ("", 10, False),
        ("«GT says absent» in the GROUND TRUTH column means the dataset annotates that field as "
         "not present on the page. A value there is a HALLUCINATION, counted separately and "
         "never folded into accuracy.", 10, False),
        ("", 10, False),
        ("Summaries use %s." % ("live formulas — they update if you filter or edit"
                                if formulas else
                                "computed values; too many rows for formulas to recalculate"),
         9, False),
    ]
    for i, (t, size, bold) in enumerate(lines, start=1):
        c = ws.cell(row=i, column=1, value=t)
        c.font = Font(name=FONT, size=size, bold=bold)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 28 if len(t) > 100 else 15
    ws.column_dimensions["A"].width = 112


if __name__ == "__main__":
    raise SystemExit(main())
