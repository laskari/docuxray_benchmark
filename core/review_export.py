#!/usr/bin/env python3
"""Export a run to Excel for key-level and file-level review.

One row per (document, field) with ground truth beside all three arms, the harness's own verdict,
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
from core.matching import load_rule_registry, compare, comparison_forms       # noqa: E402
from core.metrics import (flatten_prediction, unwrap, predicted_value,       # noqa: E402
                          load_type_registry, _resolve)
from core import rows as _rows                                               # noqa: E402
import doctypes
from registry import gt_dir as _gt_dir, REGISTRY                                                  # noqa: E402
from core.normalize import _Absent, is_emitted                                 # noqa: E402

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
SOFT = PatternFill("solid", fgColor="FFF2CC")    # passed only because of a match threshold
DIFF = PatternFill("solid", fgColor="DEEBF7")    # normalised form differs from the GT form
# Kept for reference: GT-absent now renders as a blank cell (see render()).
ABSENT_TXT = "«GT says absent»"
FORMULA_ROW_LIMIT = 5000   # above this, summaries are computed in Python (LibreOffice chokes)

# The scored arms, in pipeline order. RAW_POSTPROCESSED is a scoring construct -- production
# never builds that document -- and it is here because RAW carries no normalizedValue, so a
# RAW->FINAL difference on a number could be the product's parser rather than the judge.
ARMS = ("RAW", "RAW_POSTPROCESSED", "FINAL")
SHORT = {"RAW": "RAW", "RAW_POSTPROCESSED": "RAW_PP", "FINAL": "FINAL"}
ARM_HEADER = {
    "RAW": "RAW · judge input",
    "RAW_POSTPROCESSED": "RAW_PP · RAW through FINAL's normaliser",
    "FINAL": "FINAL · shipped output",
}
# The two transitions worth a column each. Splitting them is the point of the third arm:
# everything in RAW→RAW_PP is deterministic and free, and none of it is the judge's.
TRANSITIONS = (("d_pp", "RAW→RAW_PP", "RAW", "RAW_POSTPROCESSED"),
               ("d_judge", "RAW_PP→FINAL", "RAW_POSTPROCESSED", "FINAL"))


# WHICH KEY EACH ARM DISPLAYS. A NumericValue carries two: `originalValue`, the string the
# extractor read off the page, and `normalizedValue`, the number the product's parser made of
# it. Showing both in one cell (`24   [printed '24.000']`) put the whole pipeline in every cell
# and made the sheet hard to scan, so each arm now shows the one that matters for it:
#
#   RAW      originalValue     -- the only key it has; the judge's input
#   RAW_PP   originalValue     -- what was transcribed, before the normaliser is credited
#   FINAL    normalizedValue   -- the value that actually ships
#
# The VERDICT is still computed from whichever key core/normalize picked, which is not always
# the displayed one -- so every arm also gets a `scored on` column naming the key used. Without
# it a cell can show a correct-looking string beside a MISMATCH and look like a harness bug.
ARM_VALUE_KEY = {"RAW": "originalValue",
                 "RAW_POSTPROCESSED": "originalValue",
                 "FINAL": "normalizedValue"}


def render_arm(v: Any, prefer: str) -> str:
    """Render one arm's value, showing only `prefer` when the object carries it."""
    if v is None or isinstance(v, _Absent) or not is_emitted(v):
        return ""
    if isinstance(v, dict) and set(v) <= {"originalValue", "normalizedValue"}:
        other = "normalizedValue" if prefer == "originalValue" else "originalValue"
        chosen = v.get(prefer)
        return str(chosen) if chosen is not None else (
            "" if v.get(other) is None else str(v.get(other)))
    return render(v)


def render(v: Any) -> str:
    """Show an empty thing as empty, on BOTH sides.

    Ground truth that says ABSENT and a model object whose every leaf is null are the same
    claim -- "there is nothing here" -- so they now render as the same blank cell and a
    reviewer can compare the two columns by eye. Previously GT read "<<GT says absent>>" and
    the model read "{}", which looked like a disagreement even where the harness scored it a
    correct null. The RAW?/FINAL? verdict columns still distinguish `correct-null` from a
    blank the dataset never annotated, so nothing is lost by blanking the cell.
    """
    if v is None or isinstance(v, _Absent) or not is_emitted(v):
        return ""
    if isinstance(v, dict):
        if set(v) <= {"originalValue", "normalizedValue"}:
            # normalizedValue is the scored key (normalize.NUMERIC_SOURCE); show the printed
            # string too, but only when it disagrees -- on discountTotal the postprocessor
            # writes "(-) 9.93" against a normalizedValue of 9.93, and that gap is the single
            # most confusing thing in this sheet if it is hidden.
            o, n = v.get("originalValue"), v.get("normalizedValue")
            if n is None:
                return str(o)
            if o is None or str(o).strip() == str(n):
                return str(n)
            return f"{n}   [printed {o!r}]"
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



# ------------------------------------------------------------------- repeated sections

def force_text(ws) -> int:
    """Stop receipt text from being written as a spreadsheet FORMULA.

    openpyxl decides a cell is a formula from its value alone, so a product named '=*LARGE*=='
    -- a real item on CORD test-70 -- is serialised as one. LibreOffice then evaluates it,
    fails, and bakes '#VALUE!' into the delivered workbook where the item name should be. The
    same applies to any value beginning '=', '+', '-' or '@', which is also the classic
    spreadsheet-injection vector for text that came from outside.

    Only the evidence sheets are passed through here. They contain no formulas of ours -- every
    live formula lives on the summary sheets -- so forcing every string cell to a string type
    cannot damage anything, and the cell still displays exactly the characters the receipt had.
    """
    fixed = 0
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.data_type == "f":
                c.data_type = "s"
                fixed += 1
    return fixed


def row_cells(rec, arms_data, spec, leaf_types, judge) -> list:
    """One record per (repeated section, GT row, cell) with every arm beside it.

    WHY THIS SHEET EXISTS. The Fields sheet is built from `rec.annotated_fields`, which for a
    list path holds ONE entry -- `lineItems` -- so a receipt corpus whose whole measurement is
    line items exported 18 scalar instances and hid 119 cells. On CORD that is 87% of the
    evidence missing from the artifact the pilot is adjudicated in.

    Scoring is NOT reimplemented here: core.rows.score_rows is called per arm and its cells are
    pivoted, so this sheet and the metrics can never disagree. Alignment is recomputed per arm
    on purpose -- the model may emit different rows in RAW and FINAL, so a GT row can pair with
    different predicted rows in each, and `pred_row` is reported per arm rather than assumed.
    """
    out = []
    targets_all = (rec.meta or {}).get("row_targets") or {}
    keys_all = (rec.meta or {}).get("row_match_keys") or {}
    text_all = (rec.meta or {}).get("row_text_leaves") or {}

    for list_path in sorted(spec.list_paths):
        if list_path not in rec.annotated_fields:
            continue
        targets = targets_all.get(list_path)
        if not targets:
            continue
        gt_rows = rec.gt.get(list_path) or []
        row_targets = {k: tuple(v) for k, v in targets.items()}
        match_keys = tuple(keys_all.get(list_path) or _rows.PRED_TEXT_LEAVES)
        pred_rows, scored = {}, {}
        for arm in ARMS:
            data = arms_data.get(arm)
            if not isinstance(data, dict):
                continue
            pred_rows[arm] = _resolve(unwrap(data, spec), list_path)
            scored[arm] = _rows.score_rows(
                gt_rows, pred_rows[arm], list_path=list_path,
                gt_match_keys=match_keys,
                pred_text_leaves=tuple(text_all.get(list_path) or _rows.PRED_TEXT_LEAVES),
                row_targets=row_targets, leaf_types=leaf_types,
                doc_id=rec.doc_id, cluster_id=rec.cluster_id, spec=spec)

        # (gt_row, cell) -> per-arm cell verdict
        index = {}
        for arm, res in scored.items():
            for c in res["cells"]:
                index.setdefault((c["gt_row"], c["path"]), {})[arm] = c

        matched_gt = {arm: {m.gt_index: m for m in res["alignment"].matches}
                      for arm, res in scored.items()}

        for gi, g in enumerate(gt_rows):
            if not isinstance(g, dict):
                continue
            stated = [k for k in row_targets if g.get(k) is not None]
            # A GT row no arm could align is still evidence -- it is a row the model did not
            # find. Emitted as one line so the sheet's row count matches the GT row count and
            # a reader cannot mistake a missing row for a passing one.
            if not any(gi in matched_gt.get(arm, {}) for arm in ARMS):
                rec_row = {
                    "doc_id": rec.doc_id, "template": rec.cluster_id, "section": list_path,
                    "gt_row": gi,
                    "cell": (next((str(g[k]) for k in match_keys if g.get(k)), "")
                             or "(whole row)"),
                    "gt": render(" | ".join(f"{k}={g[k]}" for k in stated)),
                    "aligned_on": "", "matched_leaf": "",
                    "judge_flag": "", "judge_type": "", "judge_corrected": "",
                }
                for arm in ARMS:
                    rec_row[f"val_{arm}"] = ""
                    rec_row[f"v_{arm}"] = "ROW NOT FOUND" if arm in scored else ""
                    rec_row[f"s_{arm}"] = ""
                    rec_row[f"x_{arm}"] = ""
                    rec_row[f"pred_row_{arm}"] = ""
                for key, _l, _b, _a in TRANSITIONS:
                    rec_row[key] = ""
                out.append(rec_row)
                continue

            # A section whose IDENTITY keys are not among its scored cells is keyed by a
            # printed label -- 'SUBTOTAL', 'PB1', 'SVC CHG 6%'. Naming the cell 'value' there
            # throws that label away and forces the reader to pair two rows by eye. Label the
            # row with what the receipt prints instead, and only qualify it when the section
            # scores more than one cell per row (taxes carry a value AND a percentage).
            keyed_by_label = not (set(match_keys) & set(row_targets))
            label = (next((str(g[k]) for k in match_keys if g.get(k)), "")
                     if keyed_by_label else "")

            for key in stated:
                path = f"{list_path}[].{key}"
                per_arm = index.get((gi, path), {})
                cell_name = key
                if keyed_by_label and label:
                    cell_name = label if len(stated) == 1 else f"{label} · {key}"
                row = {
                    "doc_id": rec.doc_id, "template": rec.cluster_id, "section": list_path,
                    "gt_row": gi, "cell": cell_name, "gt": render(g.get(key)),
                    "aligned_on": next((c.get("matched_on") for c in per_arm.values()
                                        if c.get("matched_on")), ""),
                    "matched_leaf": next((c.get("matched_leaf") for c in per_arm.values()
                                          if c.get("matched_leaf")), ""),
                }
                issue = judge_lookup(judge, path)
                row["judge_flag"] = "yes" if issue else ""
                row["judge_type"] = (issue or {}).get("issue_type", "")
                row["judge_corrected"] = render((issue or {}).get("corrected_value"))
                for arm in ARMS:
                    c = per_arm.get(arm)
                    m = matched_gt.get(arm, {}).get(gi)
                    row[f"pred_row_{arm}"] = "" if m is None else m.pred_index
                    if arm not in scored:
                        row[f"val_{arm}"] = row[f"v_{arm}"] = ""
                        row[f"s_{arm}"] = row[f"x_{arm}"] = ""
                        continue
                    if m is None:
                        row[f"val_{arm}"] = ""
                        row[f"v_{arm}"] = "ROW NOT FOUND"
                        row[f"s_{arm}"] = row[f"x_{arm}"] = ""
                        continue
                    pr = pred_rows[arm][m.pred_index] if m.pred_index < len(pred_rows[arm]) else {}
                    leaf = (c or {}).get("matched_leaf") or key
                    row[f"s_{arm}"] = (c or {}).get("value_source") or ""
                    row[f"val_{arm}"] = render_arm(pr.get(leaf) if isinstance(pr, dict) else None,
                                                   ARM_VALUE_KEY[arm])
                    row[f"v_{arm}"] = ("match" if (c and c["ok"]) else "MISMATCH") if c else ""
                    row[f"x_{arm}"] = ""
                    if c and c["ok"]:
                        row[f"x_{arm}"] = "exact" if c.get("exact") else f"SOFT {c['score']:.3f}"
                for key_t, _label, before, after in TRANSITIONS:
                    row[key_t] = ""
                    vb, va = row.get(f"v_{before}"), row.get(f"v_{after}")
                    if vb and va:
                        okb, oka = vb == "match", va == "match"
                        row[key_t] = ("FIXED" if (not okb and oka)
                                      else ("HARMED" if (okb and not oka) else ""))
                out.append(row)
    return out


SEC_HEAD = (["doc_id", "template", "section", "GT row", "cell", "GT"]
            + [c for arm in ARMS for c in (ARM_HEADER[arm], f"v · {SHORT[arm]}",
                                           f"scored on · {SHORT[arm]}",
                                           f"exact · {SHORT[arm]}", f"pred row · {SHORT[arm]}")]
            + [label for _k, label, _b, _a in TRANSITIONS]
            + ["aligned on", "matched leaf", "judge flag", "judge type", "judge corrected",
               "your verdict", "notes"])

SEC_KEYS = (["doc_id", "template", "section", "gt_row", "cell", "gt"]
            + [c for arm in ARMS for c in (f"val_{arm}", f"v_{arm}", f"s_{arm}",
                                           f"x_{arm}", f"pred_row_{arm}")]
            + [k for k, _l, _b, _a in TRANSITIONS]
            + ["aligned_on", "matched_leaf", "judge_flag", "judge_type", "judge_corrected"])


def _rows_sheet(wb, rrows):
    """The repeated-section evidence, one line per (GT row, cell)."""
    ws = wb.create_sheet("Line items")
    ws.append(SEC_HEAD)
    for r in rrows:
        ws.append([r.get(k, "") for k in SEC_KEYS] + ["", ""])

    for cell in ws[1]:
        cell.fill, cell.font = HDR, HDRF
        cell.alignment = Alignment(vertical="top", wrap_text=True)
    verdict_cols = {SEC_HEAD.index(f"v · {SHORT[a]}") + 1 for a in ARMS}
    exact_cols = {SEC_HEAD.index(f"exact · {SHORT[a]}") + 1 for a in ARMS}
    trans_cols = {SEC_HEAD.index(l) + 1 for _k, l, _b, _a in TRANSITIONS}
    yours = SEC_HEAD.index("your verdict") + 1
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = BODY
            c.alignment = Alignment(vertical="top", wrap_text=False)
            if c.column in verdict_cols and c.value in ("MISMATCH", "ROW NOT FOUND"):
                c.fill = BAD
            elif c.column in exact_cols and isinstance(c.value, str) and c.value.startswith("SOFT"):
                c.fill = SOFT
            elif c.column in trans_cols and c.value == "FIXED":
                c.fill = GOOD
            elif c.column in trans_cols and c.value == "HARMED":
                c.fill = HARM
            elif c.column in (yours, yours + 1):
                c.fill = INPUT
        for a in ARMS:
            row[SEC_HEAD.index(ARM_HEADER[a])].font = MONO
        row[SEC_HEAD.index("GT")].font = MONO
    ws.freeze_panes = "G2"
    dv = DataValidation(type="list", allow_blank=True,
                        formula1='"GT wrong,model wrong,both defensible,alignment wrong"')
    ws.add_data_validation(dv)
    dv.add(f"{get_column_letter(yours)}2:{get_column_letter(yours)}{ws.max_row}")
    widths = {"doc_id": 12, "template": 12, "section": 20, "GT row": 7, "cell": 20, "GT": 24,
              "aligned on": 13, "matched leaf": 22, "your verdict": 18, "notes": 30}
    for a in ARMS:
        widths[f"scored on · {SHORT[a]}"] = 15
    for i, h in enumerate(SEC_HEAD, start=1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(
            h, 26 if h in ARM_HEADER.values() else 13)
    ws.auto_filter.ref = ws.dimensions
    return ws


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--dataset", default=None, choices=sorted(REGISTRY),
                    help="which ground truth to compare against; needed only when the\n"
                         "run's manifest does not record one — same flag as step7")
    a = ap.parse_args()
    run = pathlib.Path(a.run_dir)
    out = pathlib.Path(a.out) if a.out else run / f"review_{run.name}.xlsx"

    manifest = json.loads((run / "manifest.json").read_text())
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    gt_dir = _gt_dir(spec.name, a.dataset or manifest.get("dataset"))
    gt = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    leaf_types = {p.rsplit(".", 1)[-1]: t for p, t in load_type_registry(
        str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv")).items()}
    cov = {c["path"]: c for c in json.loads((gt_dir / "coverage.json").read_text())["paths"]}

    rows = []
    rrows = []
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
            cells, verdicts, exacts, sources = {}, {}, {}, {}
            for arm in ARMS:
                pred = predicted_value(path, flats[arm], spec) if arm in flats else None
                cells[arm] = pred
                exacts[arm] = ""
                sources[arm] = ""
                if arm not in flats:
                    verdicts[arm] = ""
                elif gt_absent:
                    emitted = is_emitted(pred)
                    verdicts[arm] = "HALLUCINATION" if emitted else "correct-null"
                else:
                    res = compare(pred, truth, rule)
                    verdicts[arm] = "match" if res.matched else "MISMATCH"
                    sources[arm] = res.value_source or ""
                    # A pass that is NOT exact-after-normalisation only happens under the
                    # threshold rules (text ANLS>=0.8, address, text_contained). Flagging it
                    # is the difference between "the model got this right" and "the model got
                    # close enough that the threshold let it through" -- on addresses a dropped
                    # postcode digit scores ~0.98 and reads as a clean match without this.
                    if res.matched:
                        exacts[arm] = "exact" if res.exact else f"SOFT {res.score:.3f}"
            # The normalised forms are what compare() actually equates. Computed from the
            # same (pred, truth, rule) triple that produced the verdict beside them, and
            # cross-checked against it by scripts/verify_comparison_forms.py.
            norms, norms_gt = {}, ""
            if not gt_absent:
                for arm in ARMS:
                    if arm not in flats:
                        continue
                    pf, tf = comparison_forms(cells[arm], truth, rule)
                    norms[arm] = pf
                    norms_gt = tf
            issue = judge_lookup(d.get("judge"), path)
            row = {
                "doc_id": d["doc_id"], "template": rec.cluster_id, "field": path,
                "rule": rule.value,
                "headline": "yes" if cov.get(path, {}).get("headline_eligible") else "no",
                "gt": render(truth),
                "norm_gt": norms_gt,
                "judge_flag": "yes" if issue else "",
                "judge_type": (issue or {}).get("issue_type", ""),
                "judge_corrected": render((issue or {}).get("corrected_value")),
            }
            for arm in ARMS:
                row[f"val_{arm}"] = render_arm(cells[arm], ARM_VALUE_KEY[arm])
                row[f"norm_{arm}"] = norms.get(arm, "")
                row[f"v_{arm}"] = verdicts[arm]
                row[f"s_{arm}"] = sources[arm]
                row[f"x_{arm}"] = exacts[arm]
            for key, _label, before, after in TRANSITIONS:
                row[key] = ""
                if verdicts.get(before) and verdicts.get(after):
                    okb = verdicts[before] in ("match", "correct-null")
                    oka = verdicts[after] in ("match", "correct-null")
                    row[key] = ("FIXED" if (not okb and oka)
                                else ("HARMED" if (okb and not oka) else ""))
            rows.append(row)
        rrows.extend(row_cells(rec, d.get("arms") or {}, spec, leaf_types, d.get("judge")))

    wb = Workbook()
    _fields_sheet(wb, rows, manifest)
    use_formulas = len(rows) <= FORMULA_ROW_LIMIT
    _by_file(wb, rows, use_formulas)
    _by_field(wb, rows, use_formulas)
    literal = force_text(wb["Fields"]) if "Fields" in wb.sheetnames else 0
    if rrows:
        literal += force_text(_rows_sheet(wb, rrows))
    if literal:
        print(f"{literal} cell(s) held text that starts with =/+/-/@ and were forced to "
              f"literal strings (receipt text, not formulas)")
    _readme(wb, rows, manifest, use_formulas)
    wb.save(out)
    print(f"{len(rows)} field rows · {len({r['doc_id'] for r in rows})} documents "
          f"· {len({r['field'] for r in rows})} fields")
    if rrows:
        print(f"{len(rrows)} repeated-section cells across "
              f"{len({r['section'] for r in rrows})} section(s) -> sheet 'Line items'")
    print(f"summaries use {'live formulas' if use_formulas else 'computed values (too many rows for formulas)'}")
    print(f"-> {out}")
    return 0


# Column layout, derived from ARMS rather than written out. Every formula and every
# conditional format below resolves its column BY NAME through _col/_ix. The previous version
# hardcoded letters, and its own docstring records the consequence: after an earlier column
# change the "By file" sheet's COUNTIFS still pointed at J/K/L, so "A correct" was really
# FINAL and two columns silently read zero. Adding a third arm moves nine columns.
HEADERS = (["doc_id", "template", "field", "rule", "headline", "GROUND TRUTH"]
           + [ARM_HEADER[a] for a in ARMS]
           + ["GT normalised"] + [f"{SHORT[a]} normalised" for a in ARMS]
           + [f"{SHORT[a]}?" for a in ARMS]
           + [f"{SHORT[a]} scored on" for a in ARMS]
           + [f"{SHORT[a]} exact?" for a in ARMS]
           + [label for _k, label, _b, _a in TRANSITIONS]
           + ["judge flagged", "judge issue", "judge corrected value",
              "ADJUDICATION", "NOTES"])

# Per-row cell order, matching HEADERS one for one.
ROW_KEYS = (["doc_id", "template", "field", "rule", "headline", "gt"]
            + [f"val_{a}" for a in ARMS]
            + ["norm_gt"] + [f"norm_{a}" for a in ARMS]
            + [f"v_{a}" for a in ARMS]
            + [f"s_{a}" for a in ARMS]
            + [f"x_{a}" for a in ARMS]
            + [k for k, _l, _b, _a in TRANSITIONS]
            + ["judge_flag", "judge_type", "judge_corrected"])

COL_WIDTHS = {"doc_id": 22, "template": 12, "field": 40, "rule": 14, "headline": 9,
              "GROUND TRUTH": 36, "judge flagged": 9, "judge issue": 22,
              "judge corrected value": 26, "ADJUDICATION": 20, "NOTES": 34,
              "GT normalised": 34, **{f"{SHORT[a]} normalised": 34 for a in ARMS}}


def _ix(name: str) -> int:
    """0-based column index, for row[...] access. Raises on a renamed header rather than
    silently formatting the wrong column."""
    return HEADERS.index(name)


def _col(name: str) -> str:
    """Spreadsheet column letter for a header name — the only way columns are addressed."""
    return get_column_letter(_ix(name) + 1)


def _fields_sheet(wb, rows, manifest):
    ws = wb.active; ws.title = "Fields"
    ws.append(HEADERS)
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 30
    for r in rows:
        ws.append([r.get(k, "") for k in ROW_KEYS] + ["", ""])       # + ADJUDICATION, NOTES
    mono = ([_ix("field"), _ix("GROUND TRUTH")] + [_ix(ARM_HEADER[a]) for a in ARMS]
            + [_ix("GT normalised")] + [_ix(f"{SHORT[a]} normalised") for a in ARMS]
            + [_ix("judge issue")])
    norm_ix = [_ix(f"{SHORT[a]} normalised") for a in ARMS]
    verdict_ix = [_ix(f"{SHORT[a]}?") for a in ARMS]
    exact_ix = [_ix(f"{SHORT[a]} exact?") for a in ARMS]
    delta_ix = [_ix(label) for _k, label, _b, _a in TRANSITIONS]
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.font = BODY
        for i in mono:
            row[i].font = MONO
        for i in verdict_ix:
            if row[i].value in ("MISMATCH", "HALLUCINATION"):
                row[i].fill = BAD
                row[i].font = Font(name=FONT, size=10, bold=True, color="C00000")
        for i in exact_ix:
            if str(row[i].value or "").startswith("SOFT"):
                row[i].fill = SOFT
                row[i].font = Font(name=FONT, size=10, bold=True, color="7F6000")
        for i in delta_ix:
            if row[i].value == "FIXED":
                row[i].fill = GOOD
                row[i].font = Font(name=FONT, size=10, bold=True, color="375623")
            elif row[i].value == "HARMED":
                row[i].fill = HARM
                row[i].font = Font(name=FONT, size=10, bold=True, color="C00000")
        gtn = row[_ix("GT normalised")].value
        for i in norm_ix:
            if row[i].value not in ("", None) and row[i].value != gtn:
                row[i].fill = DIFF
        row[_ix("ADJUDICATION")].fill = INPUT
        row[_ix("NOTES")].fill = INPUT
    for i, name in enumerate(HEADERS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = COL_WIDTHS.get(
            name, 36 if name in ARM_HEADER.values() else 12)
    ws.freeze_panes = f"{_col('GROUND TRUTH')}2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{ws.max_row}"
    dv = DataValidation(type="list", allow_blank=True, showDropDown=False,
                        formula1='"model_error,gt_error,normalisation_error,mapping_error,not_an_error"')
    ws.add_data_validation(dv)
    dv.add(f"{_col('ADJUDICATION')}2:{_col('ADJUDICATION')}{ws.max_row}")


def _by_file(wb, rows, formulas):
    """Per-document rates for every arm scored, and both transitions.

    History worth keeping: this sheet once spoke the retired four-arm vocabulary (A/B/C) and
    read row keys main() had stopped writing. Above FORMULA_ROW_LIMIT that raised KeyError and
    produced no sheet; below it the formula branch pointed COUNTIFS at hardcoded columns J/K/L
    -- so "A correct" was really FINAL and two columns read zero, silently. Every column here
    is now resolved by header NAME through _col(), which is why adding a third arm (nine new
    columns) does not repeat that.

    `GT present` is the rows the dataset states a value for -- match + MISMATCH on any arm,
    taken from FINAL -- and it is the denominator for every rate, matching metrics.RECALL_DEN.
    """
    ws = wb.create_sheet("By file")
    head = (["doc_id", "template", "fields", "GT present"]
            + [f"{SHORT[a]} correct" for a in ARMS]
            + [f"{SHORT[a]} rate" for a in ARMS]
            + [f"{verb} {label}" for _k, label, _b, _a in TRANSITIONS
               for verb in ("fixed", "harmed")])
    ws.append(head)
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
    n = len(rows) + 1
    docs = sorted({r["doc_id"] for r in rows})
    A, VF = "$A", _col(f"{SHORT['FINAL']}?")
    correct_at = {a: get_column_letter(head.index(f"{SHORT[a]} correct") + 1) for a in ARMS}
    present_at = get_column_letter(head.index("GT present") + 1)
    for i, doc in enumerate(docs, start=2):
        grp = [r for r in rows if r["doc_id"] == doc]
        if formulas:
            cells = [doc, grp[0]["template"],
                     f'=COUNTIF(Fields!{A}$2:{A}${n},$A{i})',
                     f'=COUNTIFS(Fields!{A}$2:{A}${n},$A{i},Fields!${VF}$2:${VF}${n},"match")'
                     f'+COUNTIFS(Fields!{A}$2:{A}${n},$A{i},Fields!${VF}$2:${VF}${n},"MISMATCH")']
            for a in ARMS:
                v = _col(f"{SHORT[a]}?")
                cells.append(
                    f'=COUNTIFS(Fields!{A}$2:{A}${n},$A{i},Fields!${v}$2:${v}${n},"match")')
            for a in ARMS:
                cells.append(f'=IFERROR({correct_at[a]}{i}/${present_at}{i},"")')
            for _k, label, _b, _a in TRANSITIONS:
                dcol = _col(label)
                for verdict in ("FIXED", "HARMED"):
                    cells.append(f'=COUNTIFS(Fields!{A}$2:{A}${n},$A{i},'
                                 f'Fields!${dcol}$2:${dcol}${n},"{verdict}")')
            ws.append(cells)
        else:
            present = sum(1 for r in grp if r["v_FINAL"] in ("match", "MISMATCH"))
            correct = {a: sum(1 for r in grp if r[f"v_{a}"] == "match") for a in ARMS}
            cells = [doc, grp[0]["template"], len(grp), present]
            cells += [correct[a] for a in ARMS]
            cells += [correct[a] / present if present else "" for a in ARMS]
            for key, _label, _b, _a in TRANSITIONS:
                cells += [sum(1 for r in grp if r[key] == "FIXED"),
                          sum(1 for r in grp if r[key] == "HARMED")]
            ws.append(cells)
    rate_cols = "".join(get_column_letter(head.index(f"{SHORT[a]} rate") + 1) for a in ARMS)
    _finish(ws, [24, 12, 8, 11] + [12] * len(ARMS) + [10] * len(ARMS)
            + [16] * (2 * len(TRANSITIONS)), pct_cols=rate_cols)


def _by_field(wb, rows, formulas):
    """Per-field rates, plus how much of FINAL's pass rate is threshold-only.

    `FINAL exact` counts the FINAL matches that were also exact after normalisation. Where it
    sits below `FINAL correct`, the gap is entirely made of text / address / text_contained
    rows that cleared a threshold without matching -- the rows to read before quoting a number.

    Rates divide by `GT present` (rows the dataset states a value for), NOT by `rows`. That is
    metrics.RECALL_DEN, so these columns reconcile with results.md. Dividing by every row would
    charge the model for correct nulls: seller.email is annotated present on 220 of its 498
    rows, and the all-rows denominator reported it at 43% against a true 98%.
    """
    ws = wb.create_sheet("By field")
    head = (["field", "rule", "headline", "rows", "GT present"]
            + [f"{SHORT[a]} correct" for a in ARMS]
            + [f"{SHORT[a]} rate" for a in ARMS]
            + ["FINAL exact", "FINAL exact rate", "threshold-only",
               "hallucinations (FINAL)"])
    ws.append(head)
    for c in ws[1]:
        c.fill, c.font = HDR, HDRF
    n = len(rows) + 1
    fields = sorted({r["field"] for r in rows})
    C, VF, XF = "$C", _col(f"{SHORT['FINAL']}?"), _col(f"{SHORT['FINAL']} exact?")
    at = {name: get_column_letter(head.index(name) + 1) for name in head}
    present_at = at["GT present"]
    fin_correct_at = at[f"{SHORT['FINAL']} correct"]
    exact_at = at["FINAL exact"]
    for i, fld in enumerate(fields, start=2):
        grp = [r for r in rows if r["field"] == fld]
        if formulas:
            cells = [fld, grp[0]["rule"], grp[0]["headline"],
                     f'=COUNTIF(Fields!{C}$2:{C}${n},$A{i})',
                     f'=COUNTIFS(Fields!{C}$2:{C}${n},$A{i},Fields!${VF}$2:${VF}${n},"match")'
                     f'+COUNTIFS(Fields!{C}$2:{C}${n},$A{i},Fields!${VF}$2:${VF}${n},"MISMATCH")']
            for a in ARMS:
                v = _col(f"{SHORT[a]}?")
                cells.append(
                    f'=COUNTIFS(Fields!{C}$2:{C}${n},$A{i},Fields!${v}$2:${v}${n},"match")')
            for a in ARMS:
                cells.append(f'=IFERROR({at[f"{SHORT[a]} correct"]}{i}/${present_at}{i},"")')
            cells += [
                f'=COUNTIFS(Fields!{C}$2:{C}${n},$A{i},Fields!${XF}$2:${XF}${n},"exact")',
                f'=IFERROR({exact_at}{i}/${present_at}{i},"")',
                f'=IFERROR({fin_correct_at}{i}-{exact_at}{i},"")',
                f'=COUNTIFS(Fields!{C}$2:{C}${n},$A{i},Fields!${VF}$2:${VF}${n},"HALLUCINATION")',
            ]
            ws.append(cells)
        else:
            present = sum(1 for r in grp if r["v_FINAL"] in ("match", "MISMATCH"))
            correct = {a: sum(1 for r in grp if r[f"v_{a}"] == "match") for a in ARMS}
            xf = sum(1 for r in grp if r["x_FINAL"] == "exact")
            cells = [fld, grp[0]["rule"], grp[0]["headline"], len(grp), present]
            cells += [correct[a] for a in ARMS]
            cells += [correct[a] / present if present else "" for a in ARMS]
            cells += [xf, xf / present if present else "", correct["FINAL"] - xf,
                      sum(1 for r in grp if r["v_FINAL"] == "HALLUCINATION")]
            ws.append(cells)
    pct = "".join(at[f"{SHORT[a]} rate"] for a in ARMS) + at["FINAL exact rate"]
    _finish(ws, [42, 14, 9, 7, 11] + [12] * len(ARMS) + [10] * len(ARMS) + [12, 16, 14, 20],
            pct_cols=pct, mono_first=True)


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
        ("RAW    · extraction + extraction_postprocessing. Exactly what the judge is shown "
         "(judge_worker.py:88), and a durable artifact in production.", 10, False),
        ("RAW_PP · RAW put through the SAME postprocessor arm FINAL ends with. A SCORING "
         "CONSTRUCT — production never builds this document and no model ever sees it. It is "
         "free (no model call) and it exists so that RAW and FINAL sit on the same side of "
         "the product's number parser.", 10, False),
        ("FINAL  · RAW plus the judge, refinement and postprocessing — the shipped output. "
         "The judge is ~74% of the bill, so RAW_PP→FINAL is what decides whether it earns "
         "its cost.", 10, False),
        ("", 10, False),
        ("Why the middle arm is there", 12, True),
        ("NumericValue declares only originalValue and sets extra=\"forbid\", so the model "
         "physically cannot emit a normalised number. RAW is therefore scored by parsing "
         "printed strings with the BENCHMARK's parser, while FINAL reads normalizedValue from "
         "the PRODUCT's. RAW→FINAL was never a clean before/after-judge comparison — it was "
         "also core/normalize vs ai/postprocessing/_common.", 10, False),
        ("Measured on runs/main: against RAW the judge looked like +73 more fixed fields than "
         "it actually was. Every one of them was totals.discountTotal, where FATURA prints "
         "\"(-) 9.39\" and the postprocessor's negative-discount abs() rule — not the judge — "
         "produces 9.39. Read RAW→RAW_PP as the deterministic, free stage and RAW_PP→FINAL as "
         "the judge and refinement together.", 10, False),
        ("", 10, False),
        ("normalised columns", 12, True),
        ("GT normalised / RAW normalised / RAW_PP normalised / FINAL normalised are the values "
         "the comparator ACTUALLY equates. The four columns to their left hold what each side "
         "stores; these hold what is left after the rule for that field type has run, which is "
         "what decides the verdict beside them. A cell is shaded pale blue when it differs from "
         "GT normalised — so scanning that block shows exactly what normalisation did and did "
         "not absorb.", 10, False),
        ("Read a matched row with a shaded stored value and an unshaded normalised value as "
         "\"normalisation closed this gap\": 3.4 against 3.40, +(833)841-9035 against "
         "+8338419035, five address components against one printed block. A MISMATCH whose two "
         "normalised cells look identical is a bug in this sheet, not a near miss — none exist: "
         "scripts/verify_comparison_forms.py replays all 84,000 arm-rows and requires "
         "(pred form == GT form) == the scorer's own exact verdict, and it passes on every one.", 10, False),
        ("Two bracketed markers appear only in numeric cells. <null> means the key was there "
         "and held null — the stage answered \"nothing\" — and <no number readable> means there "
         "was text but no number could be read out of it. Neither is ever scored correct.", 10, False),
        ("", 10, False),
        ("exact? columns", 12, True),
        ("Blank unless the arm scored a match, and since map rule 1.7 it always reads `exact` "
         "on every scored key: names and addresses are decided by equality after normalisation "
         "like everything else, so no similarity threshold can pass a row. A `SOFT 0.976` here "
         "would mean noteText, the one headline-barred field that still uses a token-recall "
         "bar.", 10, False),
        ("That rule change is why `us` against `usa` now reads MISMATCH. It used to clear a 0.90 "
         "similarity bar, which meant the benchmark forgave its own largest class of address "
         "error — 99 rows in FINAL. They are errors now, and worth fixing in the product rather "
         "than in the metric.", 10, False),
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
        ("Fields   — one row per (document, field). Filter RAW_PP→FINAL on HARMED to see "
         "every field the judge broke and on FIXED to see every one it really repaired; "
         "RAW→RAW_PP shows what the postprocessor did on its own. Filter FINAL exact? on SOFT "
         "to see every threshold-only pass.", 10, False),
        ("By file  — per-document rates. Find the documents that fail across many fields; those "
         "are usually one layout problem, not many field problems.", 10, False),
        ("By field — per-field rates for all three arms, with FINAL exact beside FINAL "
         "correct; the `threshold-only` column is the gap between them. A field where RAW rate "
         "is well below RAW_PP rate is a parser artefact, not a model failure — "
         "totals.discountTotal is the worked example. 'headline=no' fields are barred from "
         "published claims (too few distinct values, or template constants).", 10, False),
        ("", 10, False),
        ("A BLANK ground-truth cell means the dataset annotates that field as not present on "
         "the page. The arm columns then read `correct-null` (nothing emitted — right) or "
         "HALLUCINATION (something emitted — counted separately, never folded into accuracy). "
         "The model side is blanked too when every leaf is null, so the two columns can be "
         "compared by eye.", 10, False),
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
