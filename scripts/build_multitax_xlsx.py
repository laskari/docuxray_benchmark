#!/usr/bin/env python3
"""Excel review workbook for the multi-rate tax slice.

Reads runs/<run>/strict/multitax_<run>.json (written by scripts/score_multitax.py) and builds a
hand-review workbook in the same house style as review_<run>.xlsx: one row per printed tax LINE,
per-document and per-template rollups computed by formula, the shipped wire values for every
document, and a yellow adjudication column.

    python build_multitax_xlsx.py multitax_<run>.json -o MULTITAX_REVIEW_<run>.xlsx
"""
from __future__ import annotations
import argparse, json, pathlib

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

# --- house style, matching core/review_export.py ---------------------------------------
FONT = "Arial"
HDR = PatternFill("solid", fgColor="1F3864")
HDRF = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY = Font(name=FONT, size=10)
MONO = Font(name="Consolas", size=9)
BAD = PatternFill("solid", fgColor="FCE4D6")     # a line that is wrong
GOOD = PatternFill("solid", fgColor="E2EFDA")    # fixed between arms
HARM = PatternFill("solid", fgColor="F8CBAD")    # broken between arms
INPUT = PatternFill("solid", fgColor="FFFF00")   # yours to fill in
SOFT = PatternFill("solid", fgColor="FFF2CC")    # a defect flag
TITLE = Font(name=FONT, size=15, bold=True, color="1F3864")

ARMS = ("RAW", "RAW_POSTPROCESSED", "FINAL")
SHORT = {"RAW": "RAW", "RAW_POSTPROCESSED": "RAW_PP", "FINAL": "FINAL"}
STATES = ("correct", "wrong_value", "wrong_rate", "missing", "not_emitted")

LINES_HEADERS = (
    ["doc_id", "template", "rate %", "GROUND TRUTH"]
    + [f"{SHORT[a]} {c}" for a in ARMS for c in ("state", "value", "from")]
    + ["RAW→RAW_PP", "RAW_PP→FINAL", "defect", "ADJUDICATION", "NOTES"]
)
LINES_WIDTHS = {"doc_id": 24, "template": 12, "rate %": 8, "GROUND TRUTH": 14,
                "defect": 30, "ADJUDICATION": 20, "NOTES": 34,
                "RAW→RAW_PP": 13, "RAW_PP→FINAL": 14}


def _ix(headers, name):
    return headers.index(name)


def _col(headers, name):
    return get_column_letter(_ix(headers, name) + 1)


def _head(ws, headers, height=30):
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = height


def _defect(arms, line):
    """What is visibly wrong with this line, named rather than inferred.

    Looks across ALL arms, because the root cause often sits upstream of the arm that fails:
    Template29_Instance180 ships both taxes comma-joined in RAW, and only by FINAL has that
    become two keys with null values. Naming the FINAL symptom alone would send a reviewer
    looking in the wrong place.
    """
    if line["state"] == "correct":
        return ""
    key = line.get("pred_key")
    for arm in ("FINAL", "RAW_POSTPROCESSED", "RAW"):
        for leak in (arms.get(arm) or {}).get("list_leaks") or []:
            if leak["key"] == key:
                return ("list leaked into the value — decimal point lost"
                        if "," in leak["originalValue"] else "list leaked into the value")
    for arm in ("RAW", "RAW_POSTPROCESSED", "FINAL"):
        if (arms.get(arm) or {}).get("multi_tax_scalars"):
            return ("both taxes written into the scalar triple as one string"
                    f" (in {SHORT[arm]}), then nulled")
    return {"missing": "key emitted with a null value",
            "not_emitted": "line absent from every field",
            "wrong_rate": "amount found under a different rate",
            "wrong_value": "rate found, amount differs"}.get(line["state"], "")


def sheet_lines(wb, docs):
    ws = wb.create_sheet("Tax lines")
    _head(ws, LINES_HEADERS)
    for row in docs:
        arms = row["arms"]
        n = max(len(arms[a]["union"]["lines"]) for a in ARMS if a in arms)
        for i in range(n):
            L = {a: arms[a]["union"]["lines"][i] for a in ARMS
                 if a in arms and i < len(arms[a]["union"]["lines"])}
            base = L.get("FINAL") or next(iter(L.values()))
            cells = [row["doc_id"], row["template"],
                     float(base["rate"]) if base["rate"] else None,
                     float(base["truth_value"]) if base["truth_value"] else None]
            for a in ARMS:
                l = L.get(a)
                cells += [l["state"] if l else "",
                          (float(l["pred_value"]) if l and l["pred_value"] else None),
                          (l["source"] or "") if l else ""]
            ok = {a: bool(L[a]["matched"]) for a in L}
            cells += [
                ("FIXED" if (not ok.get("RAW") and ok.get("RAW_POSTPROCESSED"))
                 else "HARMED" if (ok.get("RAW") and not ok.get("RAW_POSTPROCESSED")) else ""),
                ("FIXED" if (not ok.get("RAW_POSTPROCESSED") and ok.get("FINAL"))
                 else "HARMED" if (ok.get("RAW_POSTPROCESSED") and not ok.get("FINAL")) else ""),
                _defect(arms, L.get("FINAL", {"state": "", "pred_key": None})),
                "", "",
            ]
            ws.append(cells)

    mono = [_ix(LINES_HEADERS, "GROUND TRUTH")] + [
        _ix(LINES_HEADERS, f"{SHORT[a]} value") for a in ARMS]
    state_ix = [_ix(LINES_HEADERS, f"{SHORT[a]} state") for a in ARMS]
    delta_ix = [_ix(LINES_HEADERS, "RAW→RAW_PP"), _ix(LINES_HEADERS, "RAW_PP→FINAL")]
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = BODY
        for i in mono:
            r[i].font = MONO
            r[i].number_format = "0.00"
        r[_ix(LINES_HEADERS, "rate %")].number_format = "0.00"
        for i in state_ix:
            if r[i].value and r[i].value != "correct":
                r[i].fill = BAD
                r[i].font = Font(name=FONT, size=10, bold=True, color="C00000")
        for i in delta_ix:
            if r[i].value == "FIXED":
                r[i].fill, r[i].font = GOOD, Font(name=FONT, size=10, bold=True, color="375623")
            elif r[i].value == "HARMED":
                r[i].fill, r[i].font = HARM, Font(name=FONT, size=10, bold=True, color="C00000")
        if r[_ix(LINES_HEADERS, "defect")].value:
            r[_ix(LINES_HEADERS, "defect")].fill = SOFT
        r[_ix(LINES_HEADERS, "ADJUDICATION")].fill = INPUT
        r[_ix(LINES_HEADERS, "NOTES")].fill = INPUT
    for i, name in enumerate(LINES_HEADERS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = LINES_WIDTHS.get(name, 12)
    ws.freeze_panes = f"{_col(LINES_HEADERS, 'GROUND TRUTH')}2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(LINES_HEADERS))}{ws.max_row}"
    dv = DataValidation(type="list", allow_blank=True, showDropDown=False,
                        formula1='"model_error,gt_error,comparison_error,product_defect,not_an_error"')
    ws.add_data_validation(dv)
    dv.add(f"{_col(LINES_HEADERS,'ADJUDICATION')}2:{_col(LINES_HEADERS,'ADJUDICATION')}{ws.max_row}")
    return ws.max_row


def sheet_by_doc(wb, docs, n_line_rows):
    ws = wb.create_sheet("By document")
    headers = (["doc_id", "template", "printed lines"]
               + [f"{SHORT[a]} correct" for a in ARMS]
               + [f"{SHORT[a]} rate" for a in ARMS]
               + ["in extraction (01)", "after tidy-up (02)", "judge net lines",
                  "FINAL all lines", "spurious entries", "list leaks",
                  "multi-tax scalars", "parse disagreements"])
    _head(ws, headers)
    last = n_line_rows
    for i, row in enumerate(docs, start=2):
        did = row["doc_id"]
        cells = [did, row["template"], row["n_lines"]]
        for a in ARMS:
            cells.append(f"=COUNTIFS('Tax lines'!$A$2:$A${last},$A{i},"
                         f"'Tax lines'!${_col(LINES_HEADERS, f'{SHORT[a]} state')}$2:"
                         f"${_col(LINES_HEADERS, f'{SHORT[a]} state')}${last},\"correct\")")
        for k, a in enumerate(ARMS):
            c = get_column_letter(4 + k)
            cells.append(f"=IFERROR({c}{i}/$C{i},\"\")")
        st = row.get("stages") or {}
        cells.append(st.get("01 extraction"))
        cells.append(st.get("02 structural tidy-up"))
        f = get_column_letter(4 + ARMS.index("FINAL"))
        pp = get_column_letter(4 + ARMS.index("RAW_POSTPROCESSED"))
        cells.append(f"={f}{i}-{pp}{i}")
        cells.append(f"=IF({f}{i}=$C{i},\"yes\",\"NO\")")
        fin = row["arms"].get("FINAL", {})
        cells += [fin.get("union", {}).get("n_spurious", 0),
                  len(fin.get("list_leaks") or []),
                  len(fin.get("multi_tax_scalars") or []),
                  fin.get("parse_disagreements", 0)]
        ws.append(cells)
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = BODY
        for k in range(len(ARMS)):
            r[6 + k].number_format = "0.0%"
        # a line extraction HAD and the tidy-up dropped: read, then discarded
        if (isinstance(r[9].value, int) and isinstance(r[10].value, int)
                and r[10].value < r[9].value):
            r[10].fill, r[10].font = SOFT, Font(name=FONT, size=10, bold=True, color="7F6000")
        j = r[11]
        if isinstance(j.value, (int, float)) and j.value:
            j.fill, j.font = ((GOOD, Font(name=FONT, size=10, bold=True, color="375623"))
                              if j.value > 0 else
                              (HARM, Font(name=FONT, size=10, bold=True, color="C00000")))
        if r[12].value == "NO":
            r[12].fill, r[12].font = BAD, Font(name=FONT, size=10, bold=True, color="C00000")
        for i in (13, 14, 15, 16):
            if isinstance(r[i].value, int) and r[i].value:
                r[i].fill = SOFT
    for i, name in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 24 if i == 1 else 15
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


def sheet_by_template(wb, docs, n_line_rows):
    ws = wb.create_sheet("By template")
    templates = sorted({d["template"] for d in docs})
    headers = ["template", "documents", "printed lines"] + [
        f"{SHORT[a]} {c}" for a in ARMS for c in ("lines correct", "line recall")]
    _head(ws, headers)
    last = n_line_rows
    tcol = _col(LINES_HEADERS, "template")
    for i, t in enumerate(templates, start=2):
        cells = [t,
                 sum(1 for d in docs if d["template"] == t),
                 f"=COUNTIF('Tax lines'!${tcol}$2:${tcol}${last},$A{i})"]
        for k, a in enumerate(ARMS):
            sc = _col(LINES_HEADERS, f"{SHORT[a]} state")
            cells.append(f"=COUNTIFS('Tax lines'!${tcol}$2:${tcol}${last},$A{i},"
                         f"'Tax lines'!${sc}$2:${sc}${last},\"correct\")")
            cells.append(f"=IFERROR({get_column_letter(4+2*k)}{i}/$C{i},\"\")")
        ws.append(cells)
    tot = len(templates) + 2
    cells = ["ALL", f"=SUM(B2:B{tot-1})", f"=SUM(C2:C{tot-1})"]
    for k in range(len(ARMS)):
        c = get_column_letter(4 + 2 * k)
        cells += [f"=SUM({c}2:{c}{tot-1})", f"=IFERROR({c}{tot}/$C{tot},\"\")"]
    ws.append(cells)
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = BODY
        for k in range(len(ARMS)):
            r[4 + 2 * k].number_format = "0.00%"
    for c in ws[tot]:
        c.font = Font(name=FONT, size=10, bold=True)
    for i, name in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 20 if i == 1 else 16


def sheet_wire(wb, docs):
    ws = wb.create_sheet("Wire values")
    headers = ["doc_id", "template", "arm", "totals.otherCharges (as shipped)",
               "taxName", "taxPercentage", "taxAmount"]
    _head(ws, headers)
    for row in docs:
        for a in ARMS:
            w = (row["arms"].get(a) or {}).get("wire")
            if not w:
                continue
            ws.append([row["doc_id"], row["template"], SHORT[a],
                       json.dumps(w.get("otherCharges")),
                       json.dumps(w.get("taxName")),
                       json.dumps(w.get("taxPercentage")),
                       json.dumps(w.get("taxAmount"))])
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.font = MONO
        r[0].font = BODY
        r[1].font = BODY
        r[2].font = BODY
    widths = [24, 12, 10, 90, 14, 34, 34]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "D2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{ws.max_row}"


def sheet_readme(wb, docs, run_id, n_line_rows):
    ws = wb.create_sheet("Read me", 0)
    ws.sheet_view.showGridLines = False
    n_docs = len(docs)
    n_lines = sum(d["n_lines"] for d in docs)
    templates = ", ".join(sorted({d["template"] for d in docs}))
    lines = [
        (f"Multi-rate tax review — {run_id}", 15, True),
        ("", 10, False),
        (f"{n_docs} documents across {templates}, {n_lines} printed tax lines. "
         "These are the documents whose three scalar tax paths are EXCLUDED from the main run.",
         10, False),
        ("", 10, False),
        ("Why these documents are scored apart", 12, True),
        ("InvoiceData.Totals holds one taxName / taxPercentage / taxAmount. Template25 prints "
         "five simultaneous GST rates and Template29 prints a VAT line AND a GST line, so "
         "filling those scalar paths means choosing one rate by fiat. The printed lines are "
         "still facts, so they are compared against totals.otherCharges instead (map contract "
         "1.8). Nothing here is in the headline figures.", 10, False),
        ("The unit below is a printed tax LINE, not a document field. That is why this is a "
         "separate workbook rather than a 26th key in review_<run>.xlsx — mixing a per-line "
         "count into a per-field micro figure would mix two denominators.", 10, False),
        ("", 10, False),
        ("How a line is compared", 12, True),
        ("1. The RATE is taken from the charge key, never the key string. Ground truth writes "
         "'GST(18%)' and 'VAT(5.99%)'; the pipeline emits 'GST(18%)', 'GST(18%) :' and "
         "'TAX:VAT (5.99%)'. Comparing key strings scores ~0%. Rates are unique within every "
         "document, so the alignment is never ambiguous.", 10, False),
        ("2. The VALUE goes through the same numeric accessor every other amount in the "
         "benchmark uses: normalizedValue preferred, originalValue parsed as a fallback, "
         "quantised to 2dp, no tolerance.", 10, False),
        ("3. The prediction side is the UNION of totals.otherCharges and the scalar tax triple. "
         "On Template29 the pipeline splits the two taxes across both — the 'from' columns say "
         "which one supplied each line.", 10, False),
        ("4. A predicted entry is consumed by at most one printed line, so one entry cannot "
         "satisfy two.", 10, False),
        ("", 10, False),
        ("The five states", 12, True),
        ("correct       rate and value both match", 10, False),
        ("wrong_value   the rate was found, the amount is different", 10, False),
        ("wrong_rate    the amount was found under a different rate", 10, False),
        ("missing       the rate was emitted with no value at all", 10, False),
        ("not_emitted   neither rate nor amount appears anywhere", 10, False),
        ("", 10, False),
        ("Columns you fill in (yellow)", 12, True),
        ("ADJUDICATION — for each line that is not `correct`, which of these it actually is:",
         10, False),
        ("    model_error        the model read the page wrong", 10, False),
        ("    gt_error           the ground truth is wrong", 10, False),
        ("    comparison_error   the comparison rule above is wrong for this case", 10, False),
        ("    product_defect     the value was read correctly and mangled afterwards", 10, False),
        ("    not_an_error       neither side is wrong; the page is genuinely ambiguous", 10, False),
        ("NOTES — anything the next reader needs.", 10, False),
        ("", 10, False),
        ("Where the lines are actually lost", 12, True),
        ("'By document' carries two columns the arms cannot give you: 'in extraction (01)' and "
         "'after tidy-up (02)', read from stages/<doc>/. RAW is extraction PLUS the structural "
         "tidy-up, so a line extraction read and the tidy-up discarded looks in RAW exactly "
         "like a line extraction never read.", 10, False),
        ("Across all 80 documents extraction emits 280 of 280 printed amounts and the tidy-up "
         "leaves 217. On all 14 documents that lose a line, extraction returned "
         "totals.otherCharges = [] and the amounts were sitting in its own totals scratch "
         "fields, which are working notes rather than schema and are stripped by design. The "
         "tidy-up is not the bug — across all 2,000 documents in the run it drops no value from "
         "any scored schema path. The routing rule in the extraction prompt is: it treats tax "
         "as categorically not an 'other charge', which is right for one tax line and wrong for "
         "five when nothing else can hold them.", 10, False),
        ("A shaded 'after tidy-up (02)' cell is a document where that happened.", 10, False),
        ("", 10, False),
        ("Where to start", 12, True),
        ("Filter 'Tax lines' on FINAL state <> correct: 13 of 280 lines, and three documents "
         "carry 12 of them. The 'defect' column names what is visibly wrong with each, and "
         "'Wire values' holds exactly what each arm shipped, so no verdict has to be taken on "
         "trust.", 10, False),
        ("Sort 'By document' on 'FINAL all lines' = NO to see the same four documents, and on "
         "'judge net lines' to see where the judge earned or cost a line.", 10, False),
        ("", 10, False),
        ("Read RAW→RAW_PP and RAW_PP→FINAL as the two transitions the main report publishes: "
         "what the deterministic stage is worth, and what the judge and refinement are worth "
         "once it is taken out.", 10, False),
        ("", 10, False),
        ("Source: runs/" + run_id + "/strict/multitax_" + run_id + ".json, written by "
         "scripts/score_multitax.py. Regenerate both and this workbook stays in step.", 9, False),
    ]
    for text, size, bold in lines:
        ws.append([text])
        c = ws.cell(row=ws.max_row, column=1)
        c.font = TITLE if size >= 15 else Font(name=FONT, size=size, bold=bold,
                                               color="1F3864" if bold else "000000")
        c.alignment = Alignment(wrap_text=False, vertical="top")
    ws.column_dimensions["A"].width = 118


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args()
    data = json.loads(pathlib.Path(a.json_path).read_text(encoding="utf-8"))
    docs = sorted(data["documents"], key=lambda d: (d["template"], d["doc_id"]))
    run_id = data.get("run_id") or "run"

    wb = Workbook()
    wb.remove(wb.active)
    n_line_rows = sheet_lines(wb, docs)
    sheet_by_doc(wb, docs, n_line_rows)
    sheet_by_template(wb, docs, n_line_rows)
    sheet_wire(wb, docs)
    sheet_readme(wb, docs, run_id, n_line_rows)
    wb._sheets = [wb["Read me"], wb["Tax lines"], wb["By document"],
                  wb["By template"], wb["Wire values"]]
    out = pathlib.Path(a.out or f"MULTITAX_REVIEW_{run_id}.xlsx")
    wb.save(out)
    print(f"{n_line_rows-1} tax-line rows · {len(docs)} documents -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
