"""Score a run against the frozen ground truth.

Three-state nulls (Benchmarking_plan.md section 6) are structural here, not conventional:

    recall     over paths GT says are PRESENT      -- did we get what is there?
    precision  over paths the MODEL emitted        -- was what we emitted right?
    correct-null rate                              -- its OWN line, never folded into a headline
    hallucination = emitted where GT says ABSENT   -- the damaging class, reported separately

Confidence intervals come from a bootstrap that resamples TEMPLATES, not documents. 200
instances of a FATURA template are one layout observation; resampling documents would report
intervals roughly 14x too narrow.
"""
from __future__ import annotations

import json
import pathlib
import random
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core.canonical import read_jsonl                                        # noqa: E402
from core.matching import (                                              # noqa: E402
    MatchRule,
    compare,
    load_rule_registry,
    load_type_registry,
    merge_prediction_sources,
)
import doctypes                                                          # noqa: E402
from core.normalize import _Absent                                       # noqa: E402
from registry import gt_dir as _gt_dir, dataset_for                    # noqa: E402

BOOTSTRAP_ITERS = 2000
HEADLINE_FLOOR = 20


# ---------------------------------------------------------------------------------------
# prediction access
# ---------------------------------------------------------------------------------------

def flatten_prediction(obj: Any, prefix: str = "") -> Dict[str, Any]:
    """path -> value. NumericValue objects stay whole (numeric_from_field reads them);
    addressStructured stays whole (the ADDRESS rule merges its components)."""
    out: Dict[str, Any] = {}
    if isinstance(obj, dict):
        keys = set(obj)
        if keys and keys <= {"originalValue", "normalizedValue"}:
            out[prefix] = obj
            return out
        if prefix.endswith("addressStructured"):
            out[prefix] = obj                      # keep the object for the merge
        for k, v in obj.items():
            out.update(flatten_prediction(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        out[prefix] = obj
        for i, v in enumerate(obj):
            out.update(flatten_prediction(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def unwrap(data: Dict[str, Any], spec=None) -> Dict[str, Any]:
    """Strip the doc type's wrapper key so paths match the ground truth's."""
    spec = spec or doctypes.get('invoice')
    if isinstance(data.get(spec.wrapper_key), dict):
        return data[spec.wrapper_key]
    return data


def predicted_value(path: str, flat: Dict[str, Any], spec=None) -> Any:
    spec = spec or doctypes.get('invoice')
    if path in spec.merged_targets:
        return merge_prediction_sources(path, flat, spec)
    return flat.get(path)


# ---------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------

def score_document(gt_rec, prediction: Dict[str, Any], rules, spec=None) -> List[Dict[str, Any]]:
    """One row per scoreable path. Never invents a row for a path GT cannot speak to."""
    spec = spec or doctypes.get('invoice')
    flat = flatten_prediction(unwrap(prediction, spec))
    rows = []
    for path in sorted(gt_rec.annotated_fields):
        rule = rules.get(path)
        if rule is None:
            continue
        truth = gt_rec.gt.get(path)
        pred = predicted_value(path, flat, spec)
        gt_absent = isinstance(truth, _Absent)
        emitted = pred is not None and not (isinstance(pred, str) and not pred.strip())

        if gt_absent:
            state, ok = ("hallucination", False) if emitted else ("correct_null", True)
            res = None
        else:
            res = compare(pred, truth, rule)
            state, ok = ("correct", True) if res.matched else (
                ("missing", False) if not emitted else ("wrong", False))
        rows.append({
            "doc_id": gt_rec.doc_id, "template": gt_rec.cluster_id, "path": path,
            "rule": rule.value, "state": state, "ok": ok,
            "score": (res.score if res else (1.0 if ok else 0.0)),
            "exact": bool(res.exact) if res else ok,
            "value_source": res.value_source if res else None,
            "no_tax": bool(gt_rec.meta.get("no_tax_label")),
        })
    return rows


def score_document_lists(gt_rec, prediction: Dict[str, Any], spec=None, leaf_types=None):
    """Score every repeated section this document annotates. Returns (cells, summaries).

    Kept separate from score_document because a repeated section produces TWO kinds of result
    that must not be conflated: per-cell verdicts, which join the per-path table, and a
    per-document row alignment (precision / recall / F1), which does not. Averaging cell
    accuracy without publishing row recall would let a model that emits one perfect row and
    drops nine look excellent.

    A doc type with no list_paths, or a dataset that does not annotate them, returns nothing —
    which is how FATURA passes through here untouched.
    """
    from core import rows as _rows

    spec = spec or doctypes.get("invoice")
    leaf_types = leaf_types or {}
    flat_pred = unwrap(prediction, spec)
    cells, summaries = [], []
    all_targets = gt_rec.meta.get("row_targets") or {}
    all_keys = gt_rec.meta.get("row_match_keys") or {}
    all_text = gt_rec.meta.get("row_text_leaves") or {}
    for list_path in sorted(spec.list_paths):
        if list_path not in gt_rec.annotated_fields:
            continue                      # this dataset cannot speak to this section
        targets = all_targets.get(list_path)
        if not targets:
            # The dataset annotates the section but never said which schema leaves its row
            # keys correspond to. Scoring it on guessed leaves would invent a number, so it is
            # skipped and the omission is visible in the report as a section with no rows.
            continue
        gt_rows = gt_rec.gt.get(list_path) or []
        pred_rows = _resolve(flat_pred, list_path)
        out = _rows.score_rows(
            gt_rows, pred_rows,
            list_path=list_path,
            gt_match_keys=tuple(all_keys.get(list_path) or _rows.PRED_TEXT_LEAVES),
            pred_text_leaves=tuple(all_text.get(list_path) or _rows.PRED_TEXT_LEAVES),
            row_targets={k: tuple(v) for k, v in targets.items()},
            leaf_types=leaf_types,
            doc_id=gt_rec.doc_id, cluster_id=gt_rec.cluster_id, spec=spec)
        cells.extend(out["cells"])
        summaries.append(out["summary"])
    return cells, summaries


def _resolve(obj: Any, dotted: str) -> list:
    """Follow a dotted path to a list. 'totals.otherCharges' is nested, 'lineItems' is not —
    a flat .get() silently returned nothing for the nested one and reported 0 recovery."""
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return []
        cur = cur.get(part)
    return cur if isinstance(cur, list) else []


def aggregate_lists_by_path(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One block per repeated section. Never pooled: line items and document-level charges are
    different things measured on different denominators, and one average over both is a number
    about nothing."""
    by_path: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in summaries:
        by_path[s.get("list_path", "unknown")].append(s)
    return {path: aggregate_lists(rows) for path, rows in sorted(by_path.items())}


def aggregate_lists(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Corpus-level row metrics. Micro over rows, macro over documents, both published.

    They answer different questions and can disagree sharply: micro is dominated by the 44-row
    document, macro treats every invoice as one observation. A single number here would hide
    whichever behaviour is worse.
    """
    if not summaries:
        return {}
    docs = [s for s in summaries if s.get("n_gt_rows")]
    tot_gt = sum(s["n_gt_rows"] for s in docs)
    tot_pred = sum(s["n_pred_rows"] for s in docs)
    tot_match = sum(s["n_matched"] for s in docs)
    micro_p = tot_match / tot_pred if tot_pred else None
    micro_r = tot_match / tot_gt if tot_gt else None
    macro = [s["row_f1"] for s in docs if s["row_f1"] is not None]
    cells = sum(s["n_cells"] for s in docs)
    return {
        "n_docs_with_rows": len(docs),
        "n_docs_without_rows": len(summaries) - len(docs),
        "n_gt_rows": tot_gt, "n_pred_rows": tot_pred, "n_matched_rows": tot_match,
        "micro_row_precision": micro_p,
        "micro_row_recall": micro_r,
        "micro_row_f1": (2 * micro_p * micro_r / (micro_p + micro_r)
                         if micro_p and micro_r else 0.0),
        "macro_row_f1": (sum(macro) / len(macro)) if macro else None,
        "row_exact_rate": (sum(s["n_rows_exact"] for s in docs) / tot_match
                           if tot_match else None),
        "table_exact_rate": (sum(1 for s in docs if s["table_exact"]) / len(docs)
                             if docs else None),
        "n_cells_scored": cells,
        "cell_accuracy_on_matched_rows": (sum(s["n_cells_correct"] for s in docs) / cells
                                          if cells else None),
        "note": ("Row recall and cell accuracy are independent measurements and must be read "
                 "together: cell accuracy is computed ONLY over matched rows, so a model that "
                 "drops rows raises it. Documents with no ground-truth rows are counted "
                 "separately and excluded from every row denominator."),
    }


# ---------------------------------------------------------------------------------------
# aggregation with a CLUSTER bootstrap over templates
# ---------------------------------------------------------------------------------------

def _rate(rows, numer, denom) -> Optional[float]:
    d = [r for r in rows if r["state"] in denom]
    if not d:
        return None
    return sum(1 for r in d if r["state"] in numer) / len(d)


RECALL_DEN = {"correct", "wrong", "missing"}
RECALL_NUM = {"correct"}


def cluster_bootstrap(rows, iters=BOOTSTRAP_ITERS, seed=20260901) -> Tuple[Optional[float], Optional[float]]:
    """95% interval, resampling TEMPLATES with replacement. Documents within a template are
    not independent observations -- they are one layout rendered many times."""
    by_t = defaultdict(list)
    for r in rows:
        by_t[r["template"]].append(r)
    templates = list(by_t)
    if len(templates) < 2:
        return None, None
    rng = random.Random(seed)
    samples = []
    for _ in range(iters):
        pick = [rng.choice(templates) for _ in templates]
        pooled = [r for t in pick for r in by_t[t]]
        v = _rate(pooled, RECALL_NUM, RECALL_DEN)
        if v is not None:
            samples.append(v)
    if len(samples) < 50:
        return None, None
    samples.sort()
    return samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples)) - 1]


def aggregate(rows, coverage) -> Dict[str, Any]:
    eff = {c["path"]: c for c in coverage["paths"]}
    per_field = []
    for path in sorted({r["path"] for r in rows}):
        pr = [r for r in rows if r["path"] == path]
        lo, hi = cluster_bootstrap(pr)
        c = eff.get(path, {})
        per_field.append({
            "path": path, "rule": pr[0]["rule"],
            "n_docs": len(pr), "n_templates": len({r["template"] for r in pr}),
            "recall": _rate(pr, RECALL_NUM, RECALL_DEN),
            "ci95": [lo, hi],
            "exact_rate": (sum(1 for r in pr if r["exact"] and r["state"] == "correct")
                           / max(sum(1 for r in pr if r["state"] in RECALL_DEN), 1)),
            "correct_null_rate": _rate(pr, {"correct_null"}, {"correct_null", "hallucination"}),
            "hallucinations": sum(1 for r in pr if r["state"] == "hallucination"),
            "effective_n": c.get("effective_n"),
            "headline_eligible": c.get("headline_eligible", False),
            "headline_bar_reason": c.get("headline_bar_reason"),
            "states": dict(Counter(r["state"] for r in pr)),
        })
    scored = [r for r in rows if r["state"] in RECALL_DEN]
    micro = _rate(rows, RECALL_NUM, RECALL_DEN)
    macro_vals = [f["recall"] for f in per_field if f["recall"] is not None]
    head = [f for f in per_field if f["headline_eligible"] and f["recall"] is not None]
    lo, hi = cluster_bootstrap(rows)
    return {
        "micro_recall": micro, "micro_ci95": [lo, hi],
        "macro_recall": statistics.mean(macro_vals) if macro_vals else None,
        "headline_micro_recall": _rate(
            [r for r in rows if eff.get(r["path"], {}).get("headline_eligible")],
            RECALL_NUM, RECALL_DEN),
        "n_field_instances": len(scored),
        "n_docs": len({r["doc_id"] for r in rows}),
        "n_templates": len({r["template"] for r in rows}),
        "correct_null_rate": _rate(rows, {"correct_null"}, {"correct_null", "hallucination"}),
        "hallucination_count": sum(1 for r in rows if r["state"] == "hallucination"),
        "headline_eligible_fields": len(head),
        "per_field": per_field,
    }


# ---------------------------------------------------------------------------------------
# judge: detector confusion matrix + HARM
# ---------------------------------------------------------------------------------------

def judge_metrics(rows_raw, rows_final, judge_by_doc) -> Dict[str, Any]:
    """RAW is what the judge was shown; FINAL is what the pipeline shipped.

    The detector matrix is exact -- it asks whether the judge flagged a field that was in fact
    wrong in RAW, which is precisely the input it saw. Fixed/harmed, however, span judge +
    refinement + postprocessing together, so they measure the whole post-extraction machinery
    rather than the judge alone.
    """
    b = {(r["doc_id"], r["path"]): r for r in rows_raw}
    c = {(r["doc_id"], r["path"]): r for r in rows_final}
    tp = fp = fn = tn = 0
    fixed = harmed = 0
    for key, rb in b.items():
        rc = c.get(key)
        if rc is None:
            continue
        flagged = _was_flagged(judge_by_doc.get(rb["doc_id"]), rb["path"])
        wrong_before = not rb["ok"]
        tp += flagged and wrong_before
        fp += flagged and not wrong_before
        fn += (not flagged) and wrong_before
        tn += (not flagged) and not wrong_before
        fixed += (not rb["ok"]) and rc["ok"]
        harmed += rb["ok"] and (not rc["ok"])
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    return {
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "detector_precision": prec, "detector_recall": rec,
        "detector_f1": (2 * prec * rec / (prec + rec)) if prec and rec else None,
        "fields_fixed": fixed,
        "fields_harmed": harmed,
        "net_lift_fields": fixed - harmed,
        "harm_rate": harmed / max(sum(1 for r in b.values() if r["ok"]), 1),
        "note": ("harm_rate is the share of fields CORRECT in RAW that FINAL got WRONG. "
                 "A positive net lift with a high harm rate is not a good trade. "
                 "fixed/harmed span judge + refinement + postprocessing together; the detector "
                 "matrix above is judge-specific because RAW is exactly what it was shown."),
    }


def _was_flagged(judge: Optional[dict], path: str) -> bool:
    if not judge:
        return False
    leaf = path.split(".")[-1]
    for issues in (judge.get("issues") or {}).values():
        for i in issues:
            f = str(i.get("field") or "")
            if f and (f == path or f.endswith(leaf) or leaf in f):
                return True
    return False


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

def score_run(run_dir: pathlib.Path, doc_type: str | None = None,
              dataset: str | None = None) -> Dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(doc_type or manifest.get("doc_type", "invoice"))
    # A run written before the manifest recorded its dataset leaves this null, and the
    # doc type alone can no longer name the ground truth; --dataset is the way out.
    dataset = dataset or manifest.get("dataset")
    gt_dir = _gt_dir(spec.name, dataset)
    # Whatever the manifest said, the results report the ground truth actually scored
    # against — a null here used to print "dataset None" in the header.
    manifest["dataset"] = dataset_for(spec.name, dataset).name
    gt = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    coverage = json.loads((gt_dir / "coverage.json").read_text(encoding="utf-8"))
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)

    rows_by_arm: Dict[str, list] = defaultdict(list)
    lists_by_arm: Dict[str, list] = defaultdict(list)
    cells_by_arm: Dict[str, list] = defaultdict(list)
    # leaf -> declared schema type, so a repeated section's cells get the same
    # type-driven match rule as any scalar. load_type_registry is the generated source of
    # truth; deriving rules by leaf NAME would drift the moment the schema changes.
    leaf_types = load_type_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"))
    judge_by_doc: Dict[str, dict] = {}
    n_ok = 0
    for f in sorted((run_dir / "raw").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if not d.get("ok"):
            continue
        n_ok += 1
        rec = gt.get(d["doc_id"])
        if rec is None:
            continue
        if d.get("judge"):
            judge_by_doc[d["doc_id"]] = d["judge"]
        for arm, data in (d.get("arms") or {}).items():
            if arm.startswith("_") or not isinstance(data, dict):
                continue
            rows_by_arm[arm].extend(score_document(rec, data, rules, spec))
            # Repeated-section cells are aggregated SEPARATELY and never folded into the
            # scalar field-recall denominator. On DocILE100 that denominator would otherwise be
            # 1,692 line-item cells against ~570 scalar field instances — so the headline
            # "field recall" would be three-quarters line items, and dominated by whichever
            # documents happen to have the most rows. A 44-row broadcast log would outweigh
            # forty invoices. The row-aligned section reports them on their own terms.
            cells, summaries = score_document_lists(
                rec, data, spec, leaf_types={p.rsplit(".", 1)[-1]: t
                                             for p, t in leaf_types.items()})
            cells_by_arm[arm].extend(cells)
            lists_by_arm[arm].extend(summaries)

    out: Dict[str, Any] = {
        "run_id": manifest["run_id"], "manifest": manifest,
        "n_documents_scored": n_ok,
        "field_map_version": manifest.get("field_map_version"),
        "arms": {},
    }
    for arm in sorted(rows_by_arm):
        agg = aggregate(rows_by_arm[arm], coverage)
        # The no-tax slice is a FATURA-specific correction: on that corpus 3,800 of 8,399
        # scoreable TOTAL values sit on pages with no tax label, where including-tax and
        # excluding-tax are the same figure and the distinction cannot be got wrong. It is
        # emitted only when the ground truth actually carries the flag — reporting it as
        # null on a dataset that has no such concept would invite reading it as a finding.
        # Repeated-section cells never carry it, hence .get rather than [].
        if any(r.get("no_tax") for r in rows_by_arm[arm]):
            taxed = [r for r in rows_by_arm[arm] if not r.get("no_tax")]
            agg["no_tax_slice"] = {
                "all_docs_recall": agg["micro_recall"],
                "taxed_only_recall": _rate(taxed, RECALL_NUM, RECALL_DEN),
                "note": ("3,800 of 8,399 scoreable TOTAL values sit on pages with no tax "
                         "label, where including-tax and excluding-tax are the same figure "
                         "and the distinction cannot be got wrong. Both are published."),
            }
        sections = aggregate_lists_by_path(lists_by_arm.get(arm) or [])
        if sections:
            per_cell: Dict[str, Dict[str, Any]] = defaultdict(
                lambda: {"n": 0, "correct": 0})
            for c in cells_by_arm.get(arm) or []:
                b = per_cell[c["path"]]
                b["n"] += 1
                b["correct"] += bool(c["ok"])
            for path, sec in sections.items():
                sec["per_cell"] = sorted(
                    ({"path": k, "n": v["n"], "correct": v["correct"],
                      "accuracy": v["correct"] / v["n"] if v["n"] else None}
                     for k, v in per_cell.items() if k.startswith(f"{path}[]")),
                    key=lambda d: -d["n"])
            agg["repeated_sections"] = sections
            agg["repeated_sections_per_doc"] = lists_by_arm[arm]
            if "lineItems" in sections:
                agg["line_items"] = sections["lineItems"]      # the headline section
        out["arms"][arm] = agg
    if "RAW" in rows_by_arm and "FINAL" in rows_by_arm:
        out["judge"] = judge_metrics(rows_by_arm["RAW"], rows_by_arm["FINAL"], judge_by_doc)

    (run_dir / "results.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (run_dir / "results.md").write_text(render(out), encoding="utf-8")
    return out


def render(r: Dict[str, Any]) -> str:
    m = r["manifest"]
    L = [f"# Benchmark results — {r['run_id']}", "",
         f"Field map **{r.get('field_map_version')}** · {r['n_documents_scored']} documents · "
         f"models {m['models']['extraction']} / {m['models']['judge']} · "
         f"git {m.get('git')} · cost ${m.get('total_cost_usd', 0):.4f}", "",
         f"dataset **{m.get('dataset') or 'unspecified'}** · "
         "intervals are 95% from a bootstrap resampling **clusters**, not documents — on "
         "FATURA a cluster is a template (200 instances are one layout observation); on a "
         "natural corpus such as DocILE100 the cluster is the document itself.", ""]
    for arm, a in r["arms"].items():
        ci = a["micro_ci95"]
        cis = f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci[0] is not None else ""
        L += [f"## Arm {arm}", "",
              f"- micro recall **{_pct(a['micro_recall'])}**{cis}  "
              f"({a['n_field_instances']} field instances, {a['n_docs']} docs, "
              f"{a['n_templates']} templates)",
              f"- macro recall (per field, unweighted) {_pct(a['macro_recall'])}",
              f"- headline-eligible fields only: {_pct(a['headline_micro_recall'])}",
              f"- correct-null rate {_pct(a['correct_null_rate'])}  *(own line, never a headline)*",
              f"- hallucinations (emitted where GT says absent): **{a['hallucination_count']}**"
              + ("" if a.get("hallucination_count") or "no_tax_slice" in a else
                 "  *(this dataset licenses no authoritative absence — see the map's "
                 "absence_policy; the figure is structurally zero, not a result)*"), ""]
        if "no_tax_slice" in a:
            L += [f"- no-tax slice: all docs {_pct(a['no_tax_slice']['all_docs_recall'])} vs "
                  f"taxed only **{_pct(a['no_tax_slice']['taxed_only_recall'])}**", ""]
        L += [
              "| field | rule | n | tmpl | recall | 95% CI | exact | halluc | headline |",
              "|---|---|--:|--:|--:|--|--:|--:|:--:|"]
        for f in a["per_field"]:
            lo, hi = f["ci95"]
            L.append(f"| `{f['path']}` | {f['rule']} | {f['n_docs']} | {f['n_templates']} | "
                     f"{_pct(f['recall'])} | "
                     f"{f'{lo:.2f}–{hi:.2f}' if lo is not None else '—'} | "
                     f"{_pct(f['exact_rate'])} | {f['hallucinations']} | "
                     f"{'yes' if f['headline_eligible'] else '**no**'} |")
        L.append("")
        for _sec_path, li in (a.get("repeated_sections") or {}).items():
            f2 = lambda x: "—" if x is None else _pct(x)
            L += [f"### Arm {arm} — `{_sec_path}` (row-aligned)", "",
                  f"{li['n_gt_rows']} ground-truth rows across {li['n_docs_with_rows']} "
                  f"documents; the model emitted {li['n_pred_rows']} and "
                  f"{li['n_matched_rows']} aligned. {li['n_docs_without_rows']} documents "
                  f"have no itemised table and are outside every row denominator.", "",
                  "| | micro (per row) | macro (per document) |",
                  "|---|--:|--:|",
                  f"| row precision | {f2(li['micro_row_precision'])} | — |",
                  f"| row recall | {f2(li['micro_row_recall'])} | — |",
                  f"| row F1 | {f2(li['micro_row_f1'])} | {f2(li['macro_row_f1'])} |", "",
                  f"- rows fully correct (every stated cell right, of aligned rows): "
                  f"**{f2(li['row_exact_rate'])}**",
                  f"- tables fully correct (no row missed, none invented, every cell right): "
                  f"**{f2(li['table_exact_rate'])}**",
                  f"- cell accuracy over ALIGNED rows only: {f2(li['cell_accuracy_on_matched_rows'])} "
                  f"({li['n_cells_scored']} cells)", "",
                  "Read row recall and cell accuracy together. Cell accuracy is computed only "
                  "over rows that aligned, so dropping a difficult row RAISES it — the two "
                  "numbers are only meaningful side by side, and `table_exact` is the one that "
                  "cannot be gamed by omission.", ""]
            if li.get("per_cell"):
                L += ["| cell | rows stating it | correct | accuracy |", "|---|--:|--:|--:|"]
                for c in li["per_cell"]:
                    L.append(f"| `{c['path']}` | {c['n']} | {c['correct']} | "
                             f"{_pct(c['accuracy'])} |")
                L += ["", "Each cell's denominator is the rows that ALIGNED AND state a value "
                          "for it — not the row count. A corpus-wide denominator would count "
                          "silence as failure wherever a column is sparse.", ""]
            L += ["*These cells are deliberately absent from the scalar field-recall figure "
                  "above: pooling them would let a single long table outweigh dozens of "
                  "invoices.*", ""]
    if "judge" in r:
        j = r["judge"]; c = j["confusion"]
        L += ["## Judge — as an error detector", "",
              "| | flagged | silent |", "|---|--:|--:|",
              f"| **wrong in RAW** | {c['tp']} | {c['fn']} |",
              f"| **correct in RAW** | {c['fp']} | {c['tn']} |", "",
              f"- precision {_pct(j['detector_precision'])} · recall {_pct(j['detector_recall'])} "
              f"· F1 {_pct(j['detector_f1'])}",
              f"- fields fixed **{j['fields_fixed']}** · fields harmed **{j['fields_harmed']}** "
              f"· net {j['net_lift_fields']:+d}",
              f"- **harm rate {_pct(j['harm_rate'])}** — share of fields correct in RAW that FINAL got wrong",
              "", f"> {j['note']}", ""]
    return "\n".join(L) + "\n"


def _pct(v) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    a = ap.parse_args()
    res = score_run(pathlib.Path(a.run_dir))
    print((pathlib.Path(a.run_dir) / "results.md").read_text(encoding="utf-8"))
