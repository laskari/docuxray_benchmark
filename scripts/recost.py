#!/usr/bin/env python3
"""Recompute a run's tokens and cost from artifacts already on disk. Spends nothing.

Exists because the first cost probe reported $0.00: extraction reports usage as flat
prompt_tokens/output_tokens/thinking_tokens on its metadata while the judge nests it under
token_usage, and the cost dict's total is keyed "total_cost". The runner now handles both, but
this reconstructs completed runs without re-spending.
"""
from __future__ import annotations
import json, pathlib, sys
_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from config import load                                   # noqa: E402


def stage_usage(meta: dict) -> dict:
    src = meta.get("token_usage") if isinstance(meta.get("token_usage"), dict) else meta
    pick = lambda *ks: next((int(src[k]) for k in ks if isinstance(src.get(k), (int, float))), 0)
    return {"input": pick("input", "prompt_tokens", "input_tokens"),
            "output": pick("output", "output_tokens"),
            "thinking": pick("thinking", "thinking_tokens"),
            "attempts": pick("attempts")}


def main(run_dir: str) -> int:
    run = pathlib.Path(run_dir)
    cfg = load()
    sys.path.insert(0, cfg["paths"]["ai_backend"])
    from shared.pricing import get_pricing_for_model
    price = get_pricing_for_model(cfg["models"]["extraction"])

    cache = {p.stem: json.loads(p.read_text()) for p in (_ROOT / "_cache").glob("*.json")}
    ex_by_hash = {k: v for k, v in cache.items() if k.endswith(".extract")}

    tot = {"input": 0, "output": 0, "thinking": 0}
    stages = {"extraction": dict(tot), "judge": dict(tot)}
    attempts = {"extraction": 0, "judge": 0}
    docs = 0
    for f in sorted((run / "raw").glob("*.json")):
        d = json.loads(f.read_text())
        if not d.get("ok"):
            continue
        docs += 1
        if d.get("judge"):
            u = stage_usage(d["judge"])
            for k in tot:
                stages["judge"][k] += u[k]
            attempts["judge"] += u["attempts"]
    for k, v in ex_by_hash.items():
        u = stage_usage(v.get("metadata") or {})
        for kk in tot:
            stages["extraction"][kk] += u[kk]
        attempts["extraction"] += u["attempts"]

    print(f"run {run.name}: {docs} documents\n")
    grand = 0.0
    print(f"{'stage':12s} {'input':>10s} {'output':>9s} {'thinking':>10s} {'attempts':>9s} {'cost':>9s}")
    for st, u in stages.items():
        c = u["input"] * price["input"] / 1e6 + (u["output"] + u["thinking"]) * price["output"] / 1e6
        grand += c
        for k in tot:
            tot[k] += u[k]
        print(f"{st:12s} {u['input']:>10,d} {u['output']:>9,d} {u['thinking']:>10,d} "
              f"{attempts[st]:>9d} ${c:>8.4f}")
    print(f"{'TOTAL':12s} {tot['input']:>10,d} {tot['output']:>9,d} {tot['thinking']:>10,d} "
          f"{sum(attempts.values()):>9d} ${grand:>8.4f}")
    per = grand / max(docs, 1)
    print(f"\nper document  ${per:.4f}   (thinking is {100*tot['thinking']/max(tot['output']+tot['thinking'],1):.0f}% of billed output)")
    print("\nprojection at this measured rate:")
    for name, n in (("smoke", 51), ("pilot", 99), ("variance 50x3", 150), ("main", 1000)):
        print(f"  {name:16s} {n:>5,d} docs  ${n*per:>8.2f}")
    print(f"  {'TOTAL':16s} {1300:>5,d} docs  ${1300*per:>8.2f}   cap ${cfg['run']['spend_cap_usd']:.2f}")

    obs = run / "token_usage.json"
    obs.write_text(json.dumps({
        "image_tokens": 0,
        "extraction_prompt_in": round(stages["extraction"]["input"] / max(docs * 4, 1)),
        "extraction_out": round(stages["extraction"]["output"] / max(docs * 4, 1)),
        "extraction_thinking": round(stages["extraction"]["thinking"] / max(docs * 4, 1)),
        "judge_prompt_in": round(stages["judge"]["input"] / max(docs * 5, 1)),
        "judge_out": round(stages["judge"]["output"] / max(docs * 5, 1)),
        "judge_thinking": round(stages["judge"]["thinking"] / max(docs * 5, 1)),
    }, indent=1))
    print(f"\nmeasured per-call figures -> {obs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "runs/costprobe"))
