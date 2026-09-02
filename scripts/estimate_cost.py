#!/usr/bin/env python3
"""Project the benchmark's model spend.

Reads real per-document token counts when a cost probe has been run
(runs/<run_id>/token_usage.json), otherwise falls back to the documented estimates below and
says so loudly. Prices come from the production table, shared/pricing.py, so a price change
is picked up automatically.

    PYTHONPATH=$DOCUXRAY_AI_BACKEND python3 scripts/estimate_cost.py [--probe runs/<id>/token_usage.json]
"""
from __future__ import annotations
import argparse, json, os, pathlib, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent

# --- what a run actually costs, structurally -------------------------------------------
# Arms A and B share ONE extraction: B is A plus deterministic postprocessing, which makes no
# model call. run_refinement_pipeline is also deterministic (_apply_issues). So arm C adds only
# the judge. Per document the whole A/B/C sweep is 4 extraction calls + 5 judge sections.
EXTRACTION_CALLS = 4          # SPLIT_PARTS = parties, lineitems, totals, other
JUDGE_SECTIONS   = 5          # invoiceInfo, parties, lineItems, totals, shippingInfo

# Fallback estimates. The image is resized to 1024px longest side (_MAX_IMAGE_PX), which Gemini
# tiles into roughly four 768px crops at ~258 tokens each.
EST = {
    "image_tokens":            1_100,
    "extraction_prompt_in":      850,   # measured system prompt + response schema, per part
    "extraction_out":            400,
    "extraction_thinking":     1_500,   # <-- DOMINANT UNCERTAINTY
    "judge_prompt_in":         2_500,   # section prompt + the extracted data being verified
    "judge_out":                 300,
    "judge_thinking":          1_200,   # <-- DOMINANT UNCERTAINTY
}


def per_doc(t: dict, price: dict) -> dict:
    ex_in  = EXTRACTION_CALLS * (t["image_tokens"] + t["extraction_prompt_in"])
    ex_out = EXTRACTION_CALLS * (t["extraction_out"] + t["extraction_thinking"])
    jd_in  = JUDGE_SECTIONS   * (t["image_tokens"] + t["judge_prompt_in"])
    jd_out = JUDGE_SECTIONS   * (t["judge_out"] + t["judge_thinking"])
    ex = ex_in * price["input"] / 1e6 + ex_out * price["output"] / 1e6
    jd = jd_in * price["input"] / 1e6 + jd_out * price["output"] / 1e6
    return {"extraction_usd": ex, "judge_usd": jd, "total_usd": ex + jd,
            "in_tokens": ex_in + jd_in, "out_tokens": ex_out + jd_out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", help="runs/<id>/token_usage.json from a real cost probe")
    ap.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview"))
    ap.add_argument("--smoke", type=int, default=51)
    ap.add_argument("--pilot", type=int, default=99)
    ap.add_argument("--main", type=int, default=1000)
    ap.add_argument("--variance-runs", type=int, default=3)
    ap.add_argument("--variance-docs", type=int, default=50)
    a = ap.parse_args()

    try:
        from shared.pricing import get_pricing_for_model
        price = get_pricing_for_model(a.model)
        src = "shared/pricing.py"
    except ImportError:
        price = {"input": 0.50, "output": 3.00}
        src = "hardcoded fallback (set PYTHONPATH to docuxray_ai_backend)"

    tokens, measured = dict(EST), False
    if a.probe and pathlib.Path(a.probe).exists():
        tokens.update(json.load(open(a.probe))); measured = True

    d = per_doc(tokens, price)
    docs = a.smoke + a.pilot + a.main
    variance = a.variance_docs * a.variance_runs

    print(f"model            {a.model}   (${price['input']}/M in, ${price['output']}/M out; {src})")
    print(f"token source     {'MEASURED from a cost probe' if measured else 'ESTIMATED — see the warning below'}")
    print(f"calls / document {EXTRACTION_CALLS} extraction + {JUDGE_SECTIONS} judge = "
          f"{EXTRACTION_CALLS + JUDGE_SECTIONS}\n")
    print(f"per document     {d['in_tokens']:>7,d} in  {d['out_tokens']:>7,d} out(+thinking)  "
          f"= ${d['total_usd']:.4f}")
    print(f"                 extraction ${d['extraction_usd']:.4f}  (covers arms A and B)")
    print(f"                 judge      ${d['judge_usd']:.4f}  (adds arm C)\n")
    rows = [("smoke (1 per template)", a.smoke), ("pilot", a.pilot), ("main run", a.main),
            (f"variance probe ({a.variance_docs}x{a.variance_runs})", variance)]
    total = 0.0
    for name, n in rows:
        c = n * d["total_usd"]; total += c
        print(f"  {name:34s} {n:>5,d} docs   ${c:8.2f}")
    print(f"  {'TOTAL':34s} {docs + variance:>5,d} docs   ${total:8.2f}")
    print(f"\n  re-scoring after a metric fix         0 docs   $    0.00   (response cache)")
    if not measured:
        print(
            "\n  WARNING — thinking tokens are the dominant uncertainty and are a guess here.\n"
            "  They are billed at the OUTPUT rate and are most of the bill. If real thinking is\n"
            f"  3x this estimate the total becomes roughly ${total * 2.4:.0f}.\n"
            "  Fix it by measuring, not by padding: run a 10-document probe (~$"
            f"{10 * d['total_usd']:.2f}), write the observed counts to token_usage.json, and\n"
            "  re-run with --probe. That replaces every number above with a measurement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
