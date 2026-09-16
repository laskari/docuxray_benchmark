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
from core.normalize import _Absent, is_emitted, numeric_from_field       # noqa: E402
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
        emitted = is_emitted(pred)

        if gt_absent:
            state, ok = ("hallucination", False) if emitted else ("correct_null", True)
            res = None
        else:
            res = compare(pred, truth, rule)
            # A field production nulled on purpose is MISSING, not WRONG: the pipeline dropped
            # the value, it did not misread it. Same recall either way -- both are failures --
            # but the diagnosis is the difference between "fix the model" and "fix the
            # postprocessor".
            state, ok = ("correct", True) if res.matched else (
                ("missing", False) if (not emitted or res.shipped_null) else ("wrong", False))
        rows.append({
            "doc_id": gt_rec.doc_id, "template": gt_rec.cluster_id, "path": path,
            "rule": rule.value, "state": state, "ok": ok,
            # NOTE `res is not None`, never a bare `if res`: MatchResult defines __bool__ as
            # its own verdict, so `if res` is false for every MISS and silently takes the
            # fallback branch. Harmless on `score` and `exact`, which agree with the fallback
            # on a miss; NOT harmless on the convention verdict, whose whole purpose is to
            # differ from `ok` on exactly those rows.
            "score": (res.score if res is not None else (1.0 if ok else 0.0)),
            "exact": bool(res.exact) if res is not None else ok,
            # The second verdict of contract 1.11. For a correct_null it is the same as `ok`:
            # a null the page agrees with is right under both criteria.
            "ok_convention": (bool(res.convention) if res is not None else ok),
            "value_source": res.value_source if res is not None else None,
            "no_tax": bool(gt_rec.meta.get("no_tax_label")),
        })
    import os
    if os.environ.get("DOCUXRAY_IGNORE_FIELDS"):
        ignored = set(os.environ["DOCUXRAY_IGNORE_FIELDS"].split(","))
        rows = [r for r in rows if r["path"] not in ignored]
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
        pred_rows = list(_resolve(flat_pred, list_path) or [])

        # A row the pipeline routed to a SCALAR path instead of the list is still an answer.
        # spec.list_union_sources names where to look; a source set that carries no rate
        # cannot be aligned to a printed line and is left out rather than guessed onto one.
        scalar_set = spec.list_union_sources.get(list_path)
        if scalar_set:
            name_p, pct_p, amt_p = scalar_set
            rate, _ = numeric_from_field(_dig(flat_pred, pct_p))
            if rate is not None:
                pred_rows.append({"key": f"{_dig(flat_pred, name_p) or 'TAX'}({rate}%)",
                                  "value": _dig(flat_pred, amt_p), "_from_scalar": True})

        exact_key = _rows.rate_of_row if list_path in spec.rate_keyed_lists else None
        out = _rows.score_rows(
            gt_rows, pred_rows,
            exact_key=exact_key,
            list_path=list_path,
            gt_match_keys=tuple(all_keys.get(list_path) or _rows.PRED_TEXT_LEAVES),
            pred_text_leaves=tuple(all_text.get(list_path) or _rows.PRED_TEXT_LEAVES),
            row_targets={k: tuple(v) for k, v in targets.items()},
            leaf_types=leaf_types,
            doc_id=gt_rec.doc_id, cluster_id=gt_rec.cluster_id, spec=spec)
        cells.extend(out["cells"])
        summaries.append(out["summary"])
    return cells, summaries


def _dig(obj: Any, dotted: str) -> Any:
    """Follow a dotted path through the unwrapped prediction, or None."""
    cur = obj
    for seg in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


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

def _rate_convention(rows) -> Optional[float]:
    """Convention-adjusted accuracy: the SAME denominator as `_rate(..., ACCURACY_*)`, counting
    rows the convention-adjusted criterion calls correct (map contract 1.11).

    Same denominator is the whole point -- the two numbers differ only in the numerator, so the
    gap between them is exactly the enumerated notation list and never a denominator change.
    """
    d = [r for r in rows if r["state"] in ACCURACY_DEN]
    if not d:
        return None
    return sum(1 for r in d if r.get("ok_convention")) / len(d)


def _rate(rows, numer, denom) -> Optional[float]:
    d = [r for r in rows if r["state"] in denom]
    if not d:
        return None
    return sum(1 for r in d if r["state"] in numer) / len(d)


# ACCURACY = exact matches / fields the dataset states a value for.
#
# The denominator is state (i) only -- annotated WITH a value. A field the dataset annotates as
# absent is not in it: getting a null right is reported on its own `correct_null_rate` line and
# a value emitted there is a hallucination. Folding correct nulls into the numerator would let a
# model raise its score by staying silent, which is the opposite of what this measures.
#
# `*_recall` keys are kept in results.json as aliases of the accuracy keys, because merge_runs,
# rebuild_final and the tests read them. Same number, one name in the report.
ACCURACY_DEN = {"correct", "wrong", "missing"}
ACCURACY_NUM = {"correct"}
RECALL_DEN, RECALL_NUM = ACCURACY_DEN, ACCURACY_NUM


def cluster_bootstrap(rows, iters=BOOTSTRAP_ITERS, seed=20260901,
                      convention: bool = False) -> Tuple[Optional[float], Optional[float]]:
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
        v = _rate_convention(pooled) if convention else _rate(pooled, RECALL_NUM, RECALL_DEN)
        if v is not None:
            samples.append(v)
    if len(samples) < 50:
        return None, None
    samples.sort()
    return samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples)) - 1]


def aggregate(rows, coverage, convention: bool = False) -> Dict[str, Any]:
    eff = {c["path"]: c for c in coverage["paths"]}
    per_field = []
    for path in sorted({r["path"] for r in rows}):
        pr = [r for r in rows if r["path"] == path]
        lo, hi = cluster_bootstrap(pr)
        c = eff.get(path, {})
        per_field.append({
            "path": path, "rule": pr[0]["rule"],
            "n_docs": len(pr), "n_templates": len({r["template"] for r in pr}),
            "accuracy": _rate(pr, ACCURACY_NUM, ACCURACY_DEN),
            "recall": _rate(pr, ACCURACY_NUM, ACCURACY_DEN),     # alias, same number
            "accuracy_convention": (_rate_convention(pr) if convention else None),
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
    head_rows = [r for r in rows if eff.get(r["path"], {}).get("headline_eligible")]
    conv_macro = [f["accuracy_convention"] for f in per_field
                  if f.get("accuracy_convention") is not None]
    clo, chi = cluster_bootstrap(rows, convention=True) if convention else (None, None)
    return {
        "micro_accuracy": micro, "micro_recall": micro,      # alias
        "micro_ci95": [lo, hi],
        "macro_accuracy": statistics.mean(macro_vals) if macro_vals else None,
        "macro_recall": statistics.mean(macro_vals) if macro_vals else None,   # alias
        "headline_micro_accuracy": _rate(
            [r for r in rows if eff.get(r["path"], {}).get("headline_eligible")],
            ACCURACY_NUM, ACCURACY_DEN),
        "headline_micro_recall": _rate(                                        # alias
            [r for r in rows if eff.get(r["path"], {}).get("headline_eligible")],
            ACCURACY_NUM, ACCURACY_DEN),
        "n_field_instances": len(scored),
        "n_docs": len({r["doc_id"] for r in rows}),
        "n_templates": len({r["template"] for r in rows}),
        "correct_null_rate": _rate(rows, {"correct_null"}, {"correct_null", "hallucination"}),
        "hallucination_count": sum(1 for r in rows if r["state"] == "hallucination"),
        "headline_eligible_fields": len(head),
        # ---- the convention-adjusted criterion (contract 1.11), same denominators ----
        "micro_accuracy_convention": (_rate_convention(rows) if convention else None),
        "micro_ci95_convention": [clo, chi],
        "headline_micro_accuracy_convention": (_rate_convention(head_rows) if convention else None),
        "macro_accuracy_convention": (statistics.mean(conv_macro) if conv_macro else None),
        "per_field": per_field,
    }


# ---------------------------------------------------------------------------------------
# judge: detector confusion matrix + HARM
# ---------------------------------------------------------------------------------------

def stage_lift(rows_before, rows_after, before: str, after: str) -> Dict[str, Any]:
    """fixed / harmed / net between any two arms, on the fields both scored.

    Split out of judge_metrics so the two stages after extraction can be attributed
    separately. Without a RAW -> RAW_POSTPROCESSED line the 65 fields the postprocessor
    repairs do not move to the postprocessor -- they simply vanish from the report.
    """
    b = {(r["doc_id"], r["path"]): r for r in rows_before}
    a = {(r["doc_id"], r["path"]): r for r in rows_after}
    fixed = harmed = shared = ok_before = 0
    for key, rb in b.items():
        ra = a.get(key)
        if ra is None:
            continue
        shared += 1
        ok_before += bool(rb["ok"])
        fixed += (not rb["ok"]) and ra["ok"]
        harmed += rb["ok"] and (not ra["ok"])
    return {
        "from": before, "to": after,
        "n_fields_compared": shared,
        "fields_fixed": fixed,
        "fields_harmed": harmed,
        "net_lift_fields": fixed - harmed,
        "harm_rate": harmed / max(ok_before, 1),
    }


def judge_metrics(rows_detector, rows_final, judge_by_doc,
                  rows_baseline=None, detector_arm: str = "RAW",
                  baseline_arm: str | None = None) -> Dict[str, Any]:
    """Two questions, two arms. Conflating them is an 11-point error.

    The DETECTOR matrix asks whether the judge flagged a field that was in fact wrong in the
    input the judge was SHOWN. That input is RAW -- always, whatever the baseline. Scoring the
    flags against RAW_POSTPROCESSED instead shrinks the "wrong before" denominator (169 -> 104
    on runs/main) and lifts detector recall from 29.9% to 40.9% without the judge doing
    anything differently.

    FIXED / HARMED / NET / HARM_RATE ask what the machinery after extraction changed, and there
    the baseline must be RAW_POSTPROCESSED. NumericValue sets extra="forbid", so RAW carries no
    normalizedValue and is scored by the benchmark's own string parser while FINAL is scored by
    the product's. Against RAW, 125 fields looked fixed and 46 harmed (net +79); against
    RAW_POSTPROCESSED it is 60 fixed, the same 46 harmed (net +14). The 65-field difference is
    entirely totals.discountTotal, where FATURA prints "(-) 9.39" and the postprocessor's
    negative-discount abs() rule -- not the judge -- produces 9.39.

    fixed/harmed still span judge + refinement together: refinement is a model stage that
    exists only to apply the judge's corrections and cannot be separated from it. What the
    RAW_POSTPROCESSED baseline removes is postprocessing, not refinement.
    """
    if rows_baseline is None:
        rows_baseline, baseline_arm = rows_detector, detector_arm
    baseline_arm = baseline_arm or detector_arm
    d = {(r["doc_id"], r["path"]): r for r in rows_detector}
    c = {(r["doc_id"], r["path"]): r for r in rows_final}
    tp = fp = fn = tn = 0
    for key, rd in d.items():
        if key not in c:
            continue
        flagged = _was_flagged(judge_by_doc.get(rd["doc_id"]), rd["path"])
        wrong_before = not rd["ok"]
        tp += flagged and wrong_before
        fp += flagged and not wrong_before
        fn += (not flagged) and wrong_before
        tn += (not flagged) and not wrong_before
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    lift = stage_lift(rows_baseline, rows_final, baseline_arm, "FINAL")
    return {
        "detector_arm": detector_arm,
        "baseline_arm": baseline_arm,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "detector_precision": prec, "detector_recall": rec,
        "detector_f1": (2 * prec * rec / (prec + rec)) if prec and rec else None,
        "fields_fixed": lift["fields_fixed"],
        "fields_harmed": lift["fields_harmed"],
        "net_lift_fields": lift["net_lift_fields"],
        "harm_rate": lift["harm_rate"],
        "note": (f"harm_rate is the share of fields CORRECT in {baseline_arm} that FINAL got "
                 f"WRONG. A positive net lift with a high harm rate is not a good trade. "
                 f"fixed/harmed span judge + refinement together -- refinement only applies "
                 f"the judge's corrections and cannot be separated from it; what the "
                 f"{baseline_arm} baseline removes is the postprocessor, reported on its own "
                 f"line above. The detector matrix is scored against {detector_arm} because "
                 f"that is exactly what the judge was shown."),
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
# scoring policy, and the two guards that stop a mis-scored run from being published
# ---------------------------------------------------------------------------------------

DEFAULT_SCORING = {
    # One comparison rule for every key. See core/matching.MATCH_POLICY and config.yaml.
    "match_policy": "exact",
    # Which key of a NumericValue is scored. See core.normalize.set_numeric_source. Recorded in
    # results.json so a number can never be read without knowing which side of the wire it came
    # from; it was a module constant deciding every numeric verdict silently.
    "numeric_source": "normalizedValue",
    # INERT on this doc type and kept only for provenance. Since contract 1.7 no rule reaches
    # the threshold branch -- every verdict in this benchmark is equality after normalisation.
    "anls_threshold": 0.8,
    # The second criterion published beside `exact` (map contract 1.11), when the doc type
    # opts in. Its list is CLOSED and lives in core/matching.py: a printed currency symbol
    # against its ISO code, and a country written long. Recorded here so a convention-adjusted
    # number can never be read without knowing what it forgives.
    "convention_adjusted": ["currency symbol vs ISO code", "country written long"],
    "judge_baseline_arm": "RAW_POSTPROCESSED",
    "judge_detector_arm": "RAW",
    "require_equal_arm_denominators": True,
}


def scoring_policy(override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """config.yaml's `scoring:` block, with the defaults above filling any gap.

    Read through a function rather than at import time so that the unit tests, which build
    runs in a tmpdir and never load config.yaml, keep working.
    """
    policy = dict(DEFAULT_SCORING)
    if override is None:
        try:
            import config as _config
            override = (_config.load() or {}).get("scoring")
        except Exception:                                                # noqa: BLE001
            override = None
    policy.update({k: v for k, v in (override or {}).items() if k in policy})
    # Applied HERE rather than at import time so a run always scores under the policy the
    # config declares, and so the policy lands in results.json beside the numbers it produced.
    from core.matching import set_anls_threshold, set_match_policy
    from core.normalize import set_numeric_source
    set_match_policy(policy["match_policy"])
    set_numeric_source(policy["numeric_source"])
    set_anls_threshold(policy["anls_threshold"])
    return policy


def _walk_leaves(obj: Any, prefix: str = "", out: Optional[Dict[str, Any]] = None):
    """path -> scalar, for every leaf. Unlike flatten_prediction this splits NumericValue
    into its two sub-keys, because losing `originalValue` while keeping the object is exactly
    the transition being counted."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_leaves(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_leaves(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


_INDEX_RE = __import__("re").compile(r"\[\d+\]")


def introduced_nulls(before: Any, after: Any) -> Counter:
    """Leaves that carry a value in `before` and null (or nothing) in `after`, by field path.

    The standing guard on RAW -> RAW_POSTPROCESSED. The postprocessor is not a pure
    normaliser: it also adjudicates, clearing invalid emails, out-of-range percentages,
    delivery-date status words, and anything the format contract rejects. On runs/main this is
    92 leaves of 139,618 -- 91 of them `paymentTerms.term_type: "UNKNOWN"`, plus one money
    amount the model spelled out in words. If a postprocessor change makes it 900, the judge's
    measured lift moves for a reason nothing else in the harness would surface.
    """
    b, a = _walk_leaves(before), _walk_leaves(after)
    lost: Counter = Counter()
    for path, value in b.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if a.get(path, None) is None:
            lost[_INDEX_RE.sub("[]", path)] += 1
    return lost


class ArmDenominatorMismatch(RuntimeError):
    """Raised when a run's arms do not cover an identical set of documents.

    runs/main held 1,000 raw/*.json of which only 969 carried RAW_POSTPROCESSED (the 31
    without it were the hand-substituted documents added after add_raw_pp_arm.py last ran),
    and results.md published RAW and FINAL at n=1,000 beside RAW_POSTPROCESSED at n=969 as
    though they were comparable. Nothing else in the harness would have caught it.
    """


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

class NumericSourceMismatch(Exception):
    """The dataset's stored values are not comparable against the configured numeric key."""


def score_run(run_dir: pathlib.Path, doc_type: str | None = None,
              dataset: str | None = None,
              scoring: Optional[Dict[str, Any]] = None,
              allow_arm_mismatch: bool = False,
              out_suffix: str = "",
              allow_numeric_source_mismatch: bool = False) -> Dict[str, Any]:
    """Score one run. `out_suffix` names the output files: results<suffix>.{json,md}.

    A run scored against a SECOND ground truth must not overwrite the first one's results, or
    the published numbers quietly become whichever scoring ran last. steps/step7_metrics.py
    derives the suffix from --dataset for exactly that reason.
    """
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

    # Resolved BEFORE anything is scored. It used to be resolved after the per-document loop, so
    # config.yaml's `match_policy` had no effect on the first score_run in a process: the module
    # default won and the config's value was still recorded in results.json beside numbers it did
    # not produce. Nothing was wrong while both were "exact"; setting `threshold` printed
    # exact-match numbers labelled threshold.
    policy = scoring_policy(scoring)

    rows_by_arm: Dict[str, list] = defaultdict(list)
    lists_by_arm: Dict[str, list] = defaultdict(list)
    cells_by_arm: Dict[str, list] = defaultdict(list)
    # leaf -> declared schema type, so a repeated section's cells get the same
    # type-driven match rule as any scalar. load_type_registry is the generated source of
    # truth; deriving rules by leaf NAME would drift the moment the schema changes.
    leaf_types = load_type_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"))
    judge_by_doc: Dict[str, dict] = {}
    docs_by_arm: Dict[str, set] = defaultdict(set)
    nulls_introduced: Counter = Counter()
    n_docs_with_introduced_nulls = 0
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
        arms_present = {a: v for a, v in (d.get("arms") or {}).items()
                        if not a.startswith("_") and isinstance(v, dict)}
        # The standing guard: what the postprocessor took away from RAW on its way to
        # RAW_POSTPROCESSED. Counted on the arm payloads, not on scored rows, because a leaf
        # the dataset never annotates would never appear in a row and is lost silently.
        if "RAW" in arms_present and "RAW_POSTPROCESSED" in arms_present:
            # unwrap first: whether a run stores its arms under `invoiceOutputData` is an
            # accident of which code path wrote it, and a guard that reports
            # `invoiceOutputData.invoiceInfo...` names paths that match nothing else in the
            # report -- not the coverage table, not the Excel, not the field map.
            lost = introduced_nulls(unwrap(arms_present["RAW"], spec),
                                    unwrap(arms_present["RAW_POSTPROCESSED"], spec))
            if lost:
                nulls_introduced.update(lost)
                n_docs_with_introduced_nulls += 1
        for arm, data in arms_present.items():
            docs_by_arm[arm].add(d["doc_id"])
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

    # GUARD 0 -- the dataset's value policy and the scored numeric key must agree. A verbatim
    # ground truth holds the PRINTED span, which parses to a different number than the product
    # ships ('(-) 4.35' -> -4.35 vs +4.35), so scoring it against normalizedValue publishes a
    # sign convention as a 0% field. Declared per dataset in registry.DatasetEntry.
    required = dataset_for(spec.name, dataset).requires_numeric_source
    if required and policy["numeric_source"] != required:
        message = (
            f"dataset {manifest['dataset']!r} stores values comparable only against "
            f"{required!r}, but scoring.numeric_source is {policy['numeric_source']!r}.\n"
            f"   Scoring it this way misreports formatting as reading error -- on "
            f"fatura_verbatim, totals.discountTotal lands at 0.00%.\n"
            f"   Pass --numeric-source {required} (or set scoring.numeric_source in "
            f"config.yaml).\n"
            f"   To score anyway -- and it will be wrong -- pass "
            f"--allow-numeric-source-mismatch.")
        if not allow_numeric_source_mismatch:
            raise NumericSourceMismatch(message)
        print(f"!! WARNING (--allow-numeric-source-mismatch): {message}")

    # GUARD 1 -- every arm must cover the same documents. Checked before aggregation so a
    # mismatched run cannot reach results.md at all.
    sizes = {arm: len(docs) for arm, docs in docs_by_arm.items()}
    if len(set(sizes.values())) > 1:
        biggest = max(docs_by_arm, key=lambda a: len(docs_by_arm[a]))
        detail = []
        for arm in sorted(docs_by_arm):
            missing = sorted(docs_by_arm[biggest] - docs_by_arm[arm])
            if missing:
                detail.append(f"     {arm}: missing {len(missing)} -- e.g. {missing[:4]}")
        message = (
            f"arms cover different document sets: {sizes}.\n"
            + "\n".join(detail)
            + "\n   Scoring them side by side compares different corpora. Back-fill the "
              "missing arm (scripts/add_raw_pp_arm.py) or re-run, then score again.\n"
              "   To publish anyway -- and it will be wrong -- pass --allow-arm-mismatch.")
        if policy["require_equal_arm_denominators"] and not allow_arm_mismatch:
            raise ArmDenominatorMismatch(message)
        print(f"!! WARNING (--allow-arm-mismatch): {message}")

    out: Dict[str, Any] = {
        "run_id": manifest["run_id"], "manifest": manifest,
        "n_documents_scored": n_ok,
        "field_map_version": manifest.get("field_map_version"),
        "scoring": policy,
        "arm_denominators": sizes,
        "arm_denominators_equal": len(set(sizes.values())) <= 1,
        "arms": {},
    }
    # Pipeline order, not alphabetical: sorted() put FINAL first and RAW_POSTPROCESSED last,
    # so the report read backwards and the two-line lift was impossible to follow.
    _ORDER = ("RAW", "RAW_POSTPROCESSED", "FINAL")
    for arm in (list(_ORDER) + sorted(set(rows_by_arm) - set(_ORDER))):
        if arm not in rows_by_arm:
            continue
        agg = aggregate(rows_by_arm[arm], coverage,
                        convention=getattr(spec, "convention_criterion", False))
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
    # The deterministic stage, on its own line. Its 65 fields belong to the postprocessor;
    # before this block existed they were reported as the judge's.
    if "RAW" in rows_by_arm and "RAW_POSTPROCESSED" in rows_by_arm:
        out["postprocessing"] = stage_lift(
            rows_by_arm["RAW"], rows_by_arm["RAW_POSTPROCESSED"], "RAW", "RAW_POSTPROCESSED")
        out["postprocessing"]["note"] = (
            "RAW carries no normalizedValue (NumericValue sets extra=\"forbid\"), so this "
            "line is the product's number parser plus the postprocessor's repair and "
            "adjudication rules -- no model call, no cost. Read it beside the judge block: "
            "whatever appears here is NOT the judge's.")
        out["postprocessing"]["leaves_nulled"] = {
            "total": sum(nulls_introduced.values()),
            "n_documents": n_docs_with_introduced_nulls,
            "by_field": dict(nulls_introduced.most_common()),
            "note": ("leaves that carried a value in RAW and null in RAW_POSTPROCESSED -- the "
                     "postprocessor adjudicating, not normalising. A standing guard: if this "
                     "grows, the judge's measured lift moves for an unrelated reason."),
        }

    detector_arm = policy["judge_detector_arm"]
    baseline_arm = policy["judge_baseline_arm"]
    if detector_arm in rows_by_arm and "FINAL" in rows_by_arm:
        if baseline_arm not in rows_by_arm:
            print(f"!! {baseline_arm} is not in this run; the judge's fixed/harmed fall back "
                  f"to {detector_arm}, which credits the judge with everything the "
                  f"postprocessor would have done anyway. Back-fill with "
                  f"scripts/add_raw_pp_arm.py to get the honest number.")
            baseline_arm = detector_arm
        out["judge"] = judge_metrics(
            rows_by_arm[detector_arm], rows_by_arm["FINAL"], judge_by_doc,
            rows_baseline=rows_by_arm[baseline_arm],
            detector_arm=detector_arm, baseline_arm=baseline_arm)

    (run_dir / f"results{out_suffix}.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    (run_dir / f"results{out_suffix}.md").write_text(render(out), encoding="utf-8")
    return out


def _denominator_line(r: Dict[str, Any]) -> str:
    """One line naming how many documents each arm was scored on.

    Printed unconditionally, not only on mismatch: the reason the 1,000/1,000/969 split in
    runs/main survived a read-through is that the per-arm counts were three sections apart.
    """
    sizes = r.get("arm_denominators")
    if not sizes:
        return ""
    body = " · ".join(f"{arm} {n}" for arm, n in sizes.items())
    if r.get("arm_denominators_equal", True):
        return f"documents scored per arm: {body}"
    return (f"documents scored per arm: {body} — **NOT EQUAL: these arms were scored on "
            f"different corpora and the numbers below are not comparable.**")


def render(r: Dict[str, Any]) -> str:
    m = r["manifest"]
    L = [f"# Benchmark results — {r['run_id']}", "",
         f"Field map **{r.get('field_map_version')}** · {r['n_documents_scored']} documents · "
         f"models {m['models']['extraction']} / {m['models']['judge']} · "
         f"git {m.get('git')} · cost ${m.get('total_cost_usd', 0):.4f}", "",
         _denominator_line(r), "",
         f"dataset **{m.get('dataset') or 'unspecified'}** · match policy "
         f"**{(r.get('scoring') or {}).get('match_policy')}** · numerics scored on "
         f"**{(r.get('scoring') or {}).get('numeric_source')}** · "
         "intervals are 95% from a bootstrap resampling **clusters**, not documents — on "
         "FATURA a cluster is a template (200 instances are one layout observation); on a "
         "natural corpus such as DocILE100 the cluster is the document itself.", ""]
    for arm, a in r["arms"].items():
        ci = a["micro_ci95"]
        cis = f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci[0] is not None else ""
        L += [f"## Arm {arm}", "",
              f"- micro **accuracy** **{_pct(a['micro_accuracy'])}**{cis}  "
              f"({a['n_field_instances']} field instances, {a['n_docs']} docs, "
              f"{a['n_templates']} templates)",
              f"- macro accuracy (per field, unweighted) {_pct(a['macro_accuracy'])}",
              f"- headline-eligible fields only: {_pct(a['headline_micro_accuracy'])}",]
        if a.get("micro_accuracy_convention") is not None:
            cc = a.get("micro_ci95_convention") or [None, None]
            ccs = f" [{cc[0]:.3f}, {cc[1]:.3f}]" if cc[0] is not None else ""
            L += [f"- **convention-adjusted** micro **{_pct(a['micro_accuracy_convention'])}**"
                  f"{ccs} · headline {_pct(a['headline_micro_accuracy_convention'])} · macro "
                  f"{_pct(a['macro_accuracy_convention'])}  *(same denominators; the gap is the "
                  f"closed notation list of map contract 1.11 — a printed currency symbol "
                  f"against its ISO code, and a country written long. Nothing else, and no "
                  f"threshold anywhere.)*"]
        L += [
              f"- correct-null rate {_pct(a['correct_null_rate'])}  *(own line, never a headline)*",
              f"- hallucinations (emitted where GT says absent): **{a['hallucination_count']}**"
              + ("" if a.get("hallucination_count") or "no_tax_slice" in a else
                 "  *(this dataset licenses no authoritative absence — see the map's "
                 "absence_policy; the figure is structurally zero, not a result)*"), ""]
        if "no_tax_slice" in a:
            L += [f"- no-tax slice: all docs {_pct(a['no_tax_slice']['all_docs_recall'])} vs "
                  f"taxed only **{_pct(a['no_tax_slice']['taxed_only_recall'])}**", ""]
        L += [
              "| field | rule | n | tmpl | accuracy | conv-adj | 95% CI | exact | halluc | headline |",
              "|---|---|--:|--:|--:|--:|--|--:|--:|:--:|"]
        for f in a["per_field"]:
            lo, hi = f["ci95"]
            cv = f.get("accuracy_convention")
            cvs = "—" if cv is None else (_pct(cv) if abs(cv - (f['accuracy'] or 0)) > 1e-12
                                          else "=")
            L.append(f"| `{f['path']}` | {f['rule']} | {f['n_docs']} | {f['n_templates']} | "
                     f"{_pct(f['accuracy'])} | {cvs} | "
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
    if "postprocessing" in r:
        pp = r["postprocessing"]; ln = pp["leaves_nulled"]
        L += ["## Postprocessing — the deterministic stage, on its own line", "",
              f"- `RAW` → `RAW_POSTPROCESSED` over {pp['n_fields_compared']} shared fields",
              f"- fields fixed **{pp['fields_fixed']}** · fields harmed "
              f"**{pp['fields_harmed']}** · net {pp['net_lift_fields']:+d}",
              f"- cost **$0.00** — no model call",
              f"- leaves nulled (value in RAW, null after): **{ln['total']}** across "
              f"{ln['n_documents']} documents"
              + (f" — {', '.join(f'`{k}` ×{v}' for k, v in list(ln['by_field'].items())[:4])}"
                 if ln["by_field"] else ""),
              "", f"> {pp['note']}", ""]
    if "judge" in r:
        j = r["judge"]; c = j["confusion"]
        det, base = j.get("detector_arm", "RAW"), j.get("baseline_arm", "RAW")
        L += ["## Judge — as an error detector", "",
              f"Detector matrix scored against **{det}** (what the judge was shown); "
              f"fixed/harmed against **{base}** (so the postprocessor's work is not credited "
              f"to the judge).", "",
              "| | flagged | silent |", "|---|--:|--:|",
              f"| **wrong in {det}** | {c['tp']} | {c['fn']} |",
              f"| **correct in {det}** | {c['fp']} | {c['tn']} |", "",
              f"- precision {_pct(j['detector_precision'])} · recall {_pct(j['detector_recall'])} "
              f"· F1 {_pct(j['detector_f1'])}",
              f"- fields fixed **{j['fields_fixed']}** · fields harmed **{j['fields_harmed']}** "
              f"· net {j['net_lift_fields']:+d}  *(vs {base})*",
              f"- **harm rate {_pct(j['harm_rate'])}** — share of fields correct in {base} "
              f"that FINAL got wrong",
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
