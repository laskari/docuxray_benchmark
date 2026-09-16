"""Strict-exact re-scoring of an existing run. ACCURACY ONLY.

Answers one question: what is the accuracy if the scorer's normalisation is switched OFF and
the model's emitted string must equal the ground-truth string byte for byte?

Decisions, taken by Naveen 2026-09-09:
  * Address keys are EXCLUDED. The model emits five addressStructured components against a
    single GT block, so any comparison at all requires a merge convention -- which is exactly
    the kind of normalisation this criterion removes. Scoring them would measure the merge.
  * Numerics are compared on `originalValue`, the verbatim printed string the model says it
    read, against the GT string. No 2dp quantisation, no separator handling.
  * No NFKC, no whitespace collapse, no case-fold, no date parsing, no phone digit reduction.

Nothing here writes to the run's own results.{json,md}; output goes to <run>/strict/.

The ladder: the same accuracy, recomputed with one normalisation rule restored at a time, on
one fixed denominator, so the drop can be attributed instead of merely observed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter, defaultdict

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import doctypes                                                          # noqa: E402
import core.metrics as M                                                 # noqa: E402
from core.canonical import read_jsonl                                    # noqa: E402
from core.matching import (MatchResult, MatchRule, compare as exact_compare,  # noqa: E402
                           load_rule_registry)
from core.normalize import (_Absent, normalise_date, normalise_money,    # noqa: E402
                            normalise_phone, normalise_string)
from registry import gt_dir as _gt_dir                                   # noqa: E402

ARMS = ("RAW", "RAW_POSTPROCESSED", "FINAL")

# --------------------------------------------------------------------------------------
# the ladder. Each level restores exactly one normalisation rule to the level above it.
# --------------------------------------------------------------------------------------
LEVELS = (
    "L0_strict",        # raw byte equality
    "L1_string",        # + NFKC, whitespace collapse, case-fold (text/id/enum/email/currency)
    "L2_numbers",       # + money parsing (2dp) on numeric rules
    "L3_dates",         # + ISO date parsing on date_iso
    "L4_phones",        # + phone digit reduction  == the published EXACT criterion
)


def _wire_numeric_string(value, source: str = "wire"):
    """The numeric as the arm actually SHIPS it, rendered as a string. (string|None, shipped_null).

    source="wire"          read the key the arm ships, preferring `normalizedValue` and falling
                           back to `originalValue`. This is the published scorer's own
                           NUMERIC_SOURCE order. RAW has no normalizedValue (NumericValue sets
                           extra="forbid"), so RAW is read from originalValue either way; only
                           RAW_POSTPROCESSED and FINAL change.
    source="originalValue" always the verbatim printed string.

    A `normalizedValue` is a JSON number, so its wire string is its shortest round-trip repr:
    4.6 renders "4.6", never "4.60". That is the number as shipped, not a formatted version of
    it -- formatting it to 2dp would be quantisation, i.e. the normalisation being removed.
    """
    if isinstance(value, dict):
        order = (("normalizedValue", "originalValue") if source.startswith("wire")
                 else ("originalValue",))
        for key in order:
            if key not in value:
                continue
            raw = value[key]
            if raw is None:
                # The key exists and production put null in it: an answer, not a gap.
                return None, True
            if key == "normalizedValue":
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                if source == "wire2dp":
                    # The GT is stored via money_to_str(), i.e. always 2dp. Rendering the wire
                    # number the same way is the ONE concession this variant makes, and it is
                    # a rendering rule, not a value change. Reported as its own line, never
                    # folded into the strict headline.
                    return f"{float(raw):.2f}", False
                return (str(raw) if isinstance(raw, int) else repr(raw)), False
            return str(raw), False
        return None, False
    if value is None or isinstance(value, _Absent):
        return None, False
    return str(value), False


def _as_text(value):
    return None if value is None or isinstance(value, _Absent) else str(value)


def make_compare(level: str, numeric_source: str = "wire"):
    """A compare() with normalisation restored up to `level`."""
    idx = LEVELS.index(level)

    def _cmp(prediction, truth, rule):
        from core.matching import _both_empty
        s = _both_empty(prediction, truth)
        if s is not None:
            return s

        if rule is MatchRule.NUMERIC:
            if idx >= LEVELS.index("L2_numbers"):
                from core.normalize import numeric_from_field
                prefer = "normalizedValue" if numeric_source.startswith("wire") else "originalValue"
                pv, psrc = numeric_from_field(prediction, prefer=prefer)
                tv, _ = numeric_from_field(truth)
                if pv is None:
                    return MatchResult(False, False, 0.0,
                                       reason="shipped null" if psrc else "unparseable number",
                                       value_source=psrc, shipped_null=bool(psrc))
                ok = tv is not None and pv == tv
                return MatchResult(ok, ok, float(ok), value_source=psrc)
            p, shipped_null = _wire_numeric_string(prediction, numeric_source)
            t, _ = _wire_numeric_string(truth, numeric_source)
            if p is None:
                return MatchResult(False, False, 0.0,
                                   reason="shipped null" if shipped_null else "no numeric on wire",
                                   value_source=None, shipped_null=shipped_null)
            ok = p == t
            return MatchResult(ok, ok, float(ok))

        if rule is MatchRule.DATE_ISO and idx >= LEVELS.index("L3_dates"):
            p, t = normalise_date(prediction), normalise_date(truth)
            ok = p is not None and t is not None and p == t
            return MatchResult(ok, ok, float(ok))

        if rule is MatchRule.PHONE and idx >= LEVELS.index("L4_phones"):
            ok = normalise_phone(prediction) == normalise_phone(truth)
            return MatchResult(ok, ok, float(ok))

        p, t = _as_text(prediction), _as_text(truth)
        if idx >= LEVELS.index("L1_string"):
            p, t = normalise_string(p), normalise_string(t)
        ok = p is not None and t is not None and p == t
        return MatchResult(ok, ok, float(ok))

    return _cmp


def strict_rules(spec, drop_address=True):
    rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    if drop_address:
        rules = {p: r for p, r in rules.items() if r is not MatchRule.ADDRESS}
    return rules


def score(run_dir: pathlib.Path, dataset: str, spec, rules, cmp_fn):
    """Rows per arm under one comparison function."""
    gt = {r.doc_id: r for r in read_jsonl(str(_gt_dir(spec.name, dataset) / "ground_truth.jsonl"))}
    rows_by_arm = defaultdict(list)
    saved = M.compare
    M.compare = cmp_fn
    try:
        for f in sorted((run_dir / "raw").glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            if not d.get("ok"):
                continue
            rec = gt.get(d["doc_id"])
            if rec is None:
                continue
            for arm, data in (d.get("arms") or {}).items():
                if arm.startswith("_") or not isinstance(data, dict):
                    continue
                rows_by_arm[arm].extend(M.score_document(rec, data, rules, spec))
    finally:
        M.compare = saved
    return rows_by_arm


def _tag(a):
    t = a.numeric_source
    if a.dataset:
        t += "_" + a.dataset
    if a.only:
        t += "_" + a.only
    return t


def _finish(run_dir, out, tag):
    dest = run_dir / "strict"
    dest.mkdir(exist_ok=True)
    name = f"strict_results_{tag}.json" if tag else "strict_results.json"
    (dest / name).write_text(json.dumps(out, indent=2, default=str), "utf-8")
    print("wrote", dest / name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--numeric-source", default="wire", choices=("wire", "wire2dp", "originalValue"))
    ap.add_argument("--only", default=None, help="comma-separated level names, or CONTROL")
    a = ap.parse_args()

    M.BOOTSTRAP_ITERS = a.iters
    run_dir = pathlib.Path(a.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(manifest.get("doc_type", "invoice"))
    dataset = a.dataset or manifest.get("dataset")
    coverage = json.loads((_gt_dir(spec.name, dataset) / "coverage.json").read_text("utf-8"))

    rules = strict_rules(spec)
    out = {
        "run_id": manifest["run_id"],
        "dataset": dataset,
        "criterion": "strict-exact (no normalisation); address keys excluded",
        "dataset_scored": dataset,
        "numeric_source": None,  # filled below
        "metric": "accuracy = correct / (correct + wrong + missing)",
        "n_scoreable_paths": len(rules),
        "levels": {},
        "control": {},
    }
    out["numeric_source"] = a.numeric_source

    # -------- the ladder ----------------------------------------------------------------
    want = set((a.only or ",".join(LEVELS) + ",CONTROL").split(","))
    for level in [l for l in LEVELS if l in want]:
        rows_by_arm = score(run_dir, dataset, spec, rules, make_compare(level, a.numeric_source))
        out["levels"][level] = {
            arm: M.aggregate(rows_by_arm[arm], coverage) for arm in ARMS if arm in rows_by_arm
        }
        print(f"{level:12s} " + "  ".join(
            f"{arm}={out['levels'][level][arm]['micro_accuracy']*100:.3f}" for arm in ARMS
            if arm in out["levels"][level]), flush=True)

    # -------- control: the published criterion, same code path ---------------------------
    if "CONTROL" not in want:
        _finish(run_dir, out, _tag(a)); return
    all_rules = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)
    ctrl = score(run_dir, dataset, spec, all_rules, exact_compare)
    out["control"]["published_exact_all_keys"] = {
        arm: M.aggregate(ctrl[arm], coverage) for arm in ARMS if arm in ctrl
    }
    print("control(all keys, published exact) " + "  ".join(
        f"{arm}={out['control']['published_exact_all_keys'][arm]['micro_accuracy']*100:.3f}"
        for arm in ARMS if arm in ctrl), flush=True)

    _finish(run_dir, out, _tag(a))


if __name__ == "__main__":
    main()
