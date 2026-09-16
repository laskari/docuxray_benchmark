"""Why does a field pass the published EXACT criterion but fail STRICT?

One row per (arm, path) disagreement, classified by which normalisation rule was carrying it.
Accuracy is the only metric reported anywhere; this file exists to attribute the gap, not to
add a second number.
"""
from __future__ import annotations

import json, pathlib, sys, re
from collections import Counter, defaultdict

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import doctypes
import core.metrics as M
from core.canonical import read_jsonl
from core.matching import MatchRule, compare as exact_compare, load_rule_registry
from core.normalize import _Absent, normalise_date, normalise_money, normalise_phone, normalise_string
from registry import gt_dir as _gt_dir
from scripts.score_strict import make_compare, strict_rules, ARMS

RUN = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "runs/s42_main1000")
SOURCE = sys.argv[2] if len(sys.argv) > 2 else "wire"


def classify(rule, p_raw, t_raw):
    p, t = (None if p_raw is None else str(p_raw)), (None if t_raw is None else str(t_raw))
    if p is None or t is None:
        return "one side null"
    if p == t:
        return "identical"
    if p.strip() == t.strip():
        return "leading/trailing whitespace"
    if normalise_string(p) == normalise_string(t):
        return "case / whitespace / unicode"
    if rule == "numeric":
        pv, tv = normalise_money(p), normalise_money(t)
        if pv is not None and tv is not None and pv == tv:
            sym = bool(re.search(r"[^\d.,\-+() ]", p))
            comma = "," in p and "," not in t
            paren = "(" in p and "(" not in t
            pct = "%" in p
            if pct:      return "number: percent sign"
            if paren:    return "number: accounting sign form"
            if sym:      return "number: currency symbol/code"
            if comma:    return "number: thousands separator"
            return "number: trailing zeros / decimal form"
        return "number: genuinely different value"
    if rule == "date_iso":
        if normalise_date(p) and normalise_date(p) == normalise_date(t):
            return "date: format only"
        return "date: genuinely different"
    if rule == "phone":
        if normalise_phone(p) == normalise_phone(t):
            return "phone: punctuation only"
        return "phone: genuinely different"
    return "genuinely different"


def main():
    manifest = json.loads((RUN / "manifest.json").read_text("utf-8"))
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    dataset = manifest.get("dataset")
    gt = {r.doc_id: r for r in read_jsonl(str(_gt_dir(spec.name, dataset) / "ground_truth.jsonl"))}
    rules = strict_rules(spec)
    strict_cmp = make_compare("L0_strict", SOURCE)

    reasons = defaultdict(Counter)      # arm -> reason -> n
    by_path = defaultdict(Counter)      # arm -> path -> n strict-only failures
    examples = defaultdict(list)
    tot = defaultdict(int)

    for f in sorted((RUN / "raw").glob("*.json")):
        d = json.loads(f.read_text("utf-8"))
        if not d.get("ok"):
            continue
        rec = gt.get(d["doc_id"])
        if rec is None:
            continue
        for arm, data in (d.get("arms") or {}).items():
            if arm.startswith("_") or not isinstance(data, dict):
                continue
            flat = M.flatten_prediction(M.unwrap(data, spec))
            for path in sorted(rec.annotated_fields):
                rule = rules.get(path)
                if rule is None:
                    continue
                truth = rec.gt.get(path)
                if isinstance(truth, _Absent):
                    continue
                pred = M.predicted_value(path, flat, spec)
                e = exact_compare(pred, truth, rule)
                s = strict_cmp(pred, truth, rule)
                tot[arm] += 1
                if e.matched and not s.matched:
                    if isinstance(pred, dict):
                        if SOURCE == "wire" and pred.get("normalizedValue") is not None:
                            p_raw = pred["normalizedValue"]
                            p_raw = str(p_raw) if isinstance(p_raw, int) else repr(p_raw)
                        else:
                            p_raw = pred.get("originalValue")
                    else:
                        p_raw = pred
                    why = classify(rule.value, p_raw, truth)
                    reasons[arm][why] += 1
                    by_path[arm][path] += 1
                    if len(examples[(arm, why)]) < 3:
                        examples[(arm, why)].append(
                            {"doc": rec.doc_id, "path": path, "pred": p_raw, "gt": truth})

    out = {"run": RUN.name,
           "denominator_rows_per_arm": dict(tot),
           "exact_pass_strict_fail_by_reason": {a: dict(c.most_common()) for a, c in reasons.items()},
           "exact_pass_strict_fail_by_path": {a: dict(c.most_common()) for a, c in by_path.items()},
           "examples": {f"{a} | {w}": v for (a, w), v in examples.items()}}
    dest = RUN / "strict"
    dest.mkdir(exist_ok=True)
    (dest / f"strict_diagnostics_{SOURCE}.json").write_text(json.dumps(out, indent=2, default=str), "utf-8")
    for arm in ARMS:
        if arm in reasons:
            print(arm, dict(reasons[arm].most_common(8)))
    print("wrote", dest / f"strict_diagnostics_{SOURCE}.json")


if __name__ == "__main__":
    main()
