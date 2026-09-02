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
from core.matching import (MatchRule, compare, load_rule_registry,          # noqa: E402
                          merge_prediction_sources)
import doctypes                                                             # noqa: E402
from core.normalize import _Absent                                           # noqa: E402

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

def judge_metrics(rows_b, rows_c, judge_by_doc) -> Dict[str, Any]:
    """Arm B is the baseline the judge sees; arm C is what it produced."""
    b = {(r["doc_id"], r["path"]): r for r in rows_b}
    c = {(r["doc_id"], r["path"]): r for r in rows_c}
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
        "note": ("harm_rate is the share of fields CORRECT in arm B that arm C got WRONG. "
                 "A positive net lift with a high harm rate is not a good trade."),
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

def score_run(run_dir: pathlib.Path, doc_type: str | None = None) -> Dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(doc_type or manifest.get("doc_type", "invoice"))
    gt_dir = _ROOT / "gt" / spec.name
    gt = {r.doc_id: r for r in read_jsonl(str(gt_dir / "ground_truth.jsonl"))}
    coverage = json.loads((gt_dir / "coverage.json").read_text(encoding="utf-8"))
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)

    rows_by_arm: Dict[str, list] = defaultdict(list)
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

    out: Dict[str, Any] = {
        "run_id": manifest["run_id"], "manifest": manifest,
        "n_documents_scored": n_ok,
        "field_map_version": manifest.get("field_map_version"),
        "arms": {},
    }
    for arm in sorted(rows_by_arm):
        agg = aggregate(rows_by_arm[arm], coverage)
        taxed = [r for r in rows_by_arm[arm] if not r["no_tax"]]
        agg["no_tax_slice"] = {
            "all_docs_recall": agg["micro_recall"],
            "taxed_only_recall": _rate(taxed, RECALL_NUM, RECALL_DEN),
            "note": ("3,800 of 8,399 scoreable TOTAL values sit on pages with no tax label, "
                     "where including-tax and excluding-tax are the same figure and the "
                     "distinction cannot be got wrong. Both numbers are published."),
        }
        out["arms"][arm] = agg
    if "B" in rows_by_arm and "C" in rows_by_arm:
        out["judge"] = judge_metrics(rows_by_arm["B"], rows_by_arm["C"], judge_by_doc)

    (run_dir / "results.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (run_dir / "results.md").write_text(render(out), encoding="utf-8")
    return out


def render(r: Dict[str, Any]) -> str:
    m = r["manifest"]
    L = [f"# Benchmark results — {r['run_id']}", "",
         f"Field map **{r.get('field_map_version')}** · {r['n_documents_scored']} documents · "
         f"models {m['models']['extraction']} / {m['models']['judge']} · "
         f"git {m.get('git')} · cost ${m.get('total_cost_usd', 0):.4f}", "",
         "Intervals are 95% from a bootstrap resampling **templates**, not documents — 200 "
         "instances of a FATURA template are one layout observation.", ""]
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
              f"- hallucinations (emitted where GT says absent): **{a['hallucination_count']}**",
              f"- no-tax slice: all docs {_pct(a['no_tax_slice']['all_docs_recall'])} vs "
              f"taxed only **{_pct(a['no_tax_slice']['taxed_only_recall'])}**", "",
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
    if "judge" in r:
        j = r["judge"]; c = j["confusion"]
        L += ["## Judge — as an error detector", "",
              "| | flagged | silent |", "|---|--:|--:|",
              f"| **wrong in arm B** | {c['tp']} | {c['fn']} |",
              f"| **correct in arm B** | {c['fp']} | {c['tn']} |", "",
              f"- precision {_pct(j['detector_precision'])} · recall {_pct(j['detector_recall'])} "
              f"· F1 {_pct(j['detector_f1'])}",
              f"- fields fixed **{j['fields_fixed']}** · fields harmed **{j['fields_harmed']}** "
              f"· net {j['net_lift_fields']:+d}",
              f"- **harm rate {_pct(j['harm_rate'])}** — share of fields correct in B that C got wrong",
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
