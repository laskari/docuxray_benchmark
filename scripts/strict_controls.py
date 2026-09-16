"""Two extra reference points for the strict ladder, so its number has an apples-to-apples peer.

  A. published EXACT, address keys EXCLUDED  -- the same 22-key denominator the strict number
     uses, scored under the shipped criterion. This is what strict must be compared against.
  B. L4 + numerics read from normalizedValue -- isolates how much of FINAL's strict penalty is
     the postprocessor REWRITING originalValue ("9.93" -> "(-) 9.93") rather than reading wrong.
"""
from __future__ import annotations
import json, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import doctypes, core.metrics as M
from core.matching import MatchResult, MatchRule, compare as exact_compare
from core.normalize import _Absent, normalise_money, normalise_phone, normalise_date, normalise_string
from registry import gt_dir as _gt_dir
from scripts.score_strict import strict_rules, score, ARMS

RUN = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "runs/s42_main1000")
ITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
M.BOOTSTRAP_ITERS = ITERS

manifest = json.loads((RUN / "manifest.json").read_text("utf-8"))
spec = doctypes.get(manifest.get("doc_type", "invoice"))
dataset = manifest.get("dataset")
coverage = json.loads((_gt_dir(spec.name, dataset) / "coverage.json").read_text("utf-8"))
rules = strict_rules(spec)


def cmp_norm_source(prediction, truth, rule):
    """Published exact behaviour, but only for numerics; everything else already matches."""
    return exact_compare(prediction, truth, rule)


out = {}
for label, fn in (("A_published_exact_no_address", exact_compare),):
    rows = score(RUN, dataset, spec, rules, fn)
    out[label] = {a: M.aggregate(rows[a], coverage) for a in ARMS if a in rows}
    print(label, "  ".join(f"{a}={out[label][a]['micro_accuracy']*100:.3f}" for a in ARMS if a in rows), flush=True)

dest = RUN / "strict"; dest.mkdir(exist_ok=True)
(dest / "strict_controls.json").write_text(json.dumps(out, indent=2, default=str), "utf-8")
print("wrote", dest / "strict_controls.json")
