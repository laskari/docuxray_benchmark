#!/usr/bin/env python3
"""Audit a sampling plan: reproduce it from a seed, and diff it against what was RUN.

Answers three questions without spending anything:

  * does a given (seed, per-cluster N, draw mode) reproduce a plan file on disk?
  * how many documents of a candidate plan are already in a completed run, and so free?
  * is the N=2k plan a superset of the N=k plan under the same seed?

The draw mode matters and is the whole reason this exists. `rng.sample(pop, k)` and
`rng.shuffle(pop)[:k]` are BOTH "random k from pop with this seed" and they return
DIFFERENT documents -- and sample() is not nested across k, while shuffle() is. The
refactor of build_gt.py swapped sample() for shuffle(), which silently re-drew every
plan. Nothing in the plan file records which one produced it, so it has to be recovered
by replay.

    python scripts/audit_plan_diff.py --identify gt/invoice/fatura/main_1000.json
    python scripts/audit_plan_diff.py --seed 42 --per 20 --mode shuffle --against-run runs/main
    python scripts/audit_plan_diff.py --seed 42 --per 40 --mode shuffle --nesting 20 \
        --write gt/invoice/fatura/candidate_2000.json

    # draw 2,000 and cut it into four runnable 500s that nest
    python scripts/audit_plan_diff.py --seed 42 --per 40 --mode shuffle \
        --write gt/invoice/fatura/main_2000_seed42.json --slices 4 \
        --slice-name gt/invoice/fatura/main_500_seed42_slice{i}.json
"""
from __future__ import annotations
import argparse, collections, glob, hashlib, json, os, pathlib, random, subprocess, sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
COST_PER_DOC_USD = 0.0724      # measured: mean of 990 fresh FATURA docs in runs/main, both arms


def load_gt(gt_dir: pathlib.Path):
    recs = [json.loads(l) for l in (gt_dir / "ground_truth.jsonl").open(encoding="utf-8")]
    by_cluster, by_keyset = collections.defaultdict(list), collections.defaultdict(list)
    for r in recs:
        by_cluster[r["cluster_id"]].append(r["doc_id"])
        by_keyset[r["keyset_id"]].append(r["doc_id"])
    return recs, by_cluster, by_keyset


def burn_pilot(rng, recs, by_keyset, pilot_size: int, rows_key: str):
    """Replay build_gt.py's pilot construction PURELY for its RNG consumption.

    build_gt.py draws the pilot from the same Random instance before it draws the main
    plan, so the main plan depends on how much randomness the pilot ate. It reads
    meta['n_gt_rows'], which FATURA's adapter does not emit (it writes
    'gt_line_item_rows'), so every document strata-sorts to 'none' and exactly one
    rng.sample(10000-list, 2) is consumed. Change either and every main plan moves.
    """
    pilot = [sorted(v)[len(v) // 2] for v in by_keyset.values()]
    strata = collections.defaultdict(list)
    for r in recs:
        rows = int((r.get("meta") or {}).get(rows_key) or 0)
        strata["none" if rows == 0 else "single" if rows == 1 else
               "small" if rows <= 3 else "medium" if rows <= 9 else "large"].append(r["doc_id"])
    for name in ("none", "single", "small", "medium", "large"):
        for doc in rng.sample(sorted(strata[name]), min(2, len(strata[name]))):
            if len(pilot) >= pilot_size:
                break
            if doc not in pilot:
                pilot.append(doc)
    return sorted(pilot)


def draw_by_cluster(by_cluster, seed, per, mode, *, burn=None):
    """cluster -> the `per` documents drawn from it, IN DRAW ORDER.

    Draw order is the whole point of returning this rather than a flat sorted list: slicing a
    plan by POSITION within each cluster is what makes the slices nest. Slice k is
    order[k*size:(k+1)*size] for every cluster, so each slice is balanced across templates by
    construction, slices 1..j are exactly the (j*size)-per-cluster draw, and every prefix is a
    valid smaller plan of the same design.
    """
    rng = random.Random(seed)
    if burn:
        burn(rng)
    picked = {}
    for c in sorted(by_cluster):
        docs = sorted(by_cluster[c])
        if mode == "shuffle":
            rng.shuffle(docs)
            picked[c] = docs[:per]
        elif mode == "sample":
            # rng.sample draws a fresh combination for every k: sample(pop, 20) is NOT a
            # prefix of sample(pop, 40), so a plan drawn this way cannot be sliced or grown.
            # Kept only to reproduce the plans already on disk.
            picked[c] = rng.sample(docs, min(per, len(docs)))
        else:
            raise ValueError(mode)
    return picked


def draw(by_cluster, seed, per, mode, *, burn=None):
    picked = draw_by_cluster(by_cluster, seed, per, mode, burn=burn)
    return sorted(d for docs in picked.values() for d in docs)


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def provenance(gt_dir: pathlib.Path, seed: int, per: int, mode: str, burn: bool,
               pilot_size: int, rows_key: str) -> dict:
    """Everything needed to redraw this plan and prove it is the same plan.

    A plan that records only its seed is not reproducible. `main_1000.json` recorded
    `"seed": 42` and nothing else, and the refactor of build_gt.py from rng.sample to
    rng.shuffle silently redrew it under the same seed and the same filename -- 104 of 1,000
    documents in common. The draw mode, the RNG burned by the pilot before the main draw, and
    the ground truth the clusters came from are all part of the answer.
    """
    def _git(*args):
        try:
            return subprocess.run(["git", "-C", str(_ROOT), *args],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:                                                # noqa: BLE001
            return ""
    return {
        "seed": seed,
        "per_cluster": per,
        "draw_mode": mode,
        "pilot_burn": burn,
        "pilot_size": pilot_size,
        "pilot_strata_key": rows_key,
        "ground_truth_sha256": _sha256(gt_dir / "ground_truth.jsonl"),
        "drawn_by": "scripts/audit_plan_diff.py",
        "drawn_by_sha256": hashlib.sha256(
            pathlib.Path(__file__).read_bytes()).hexdigest()[:16],
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty_paths": [l[3:] for l in _git("status", "--porcelain").splitlines()
                            if l[3:] in ("scripts/build_gt.py", "scripts/audit_plan_diff.py",
                                         "steps/step2_ground_truth.py")],
    }


def plan_ids(path) -> set:
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return set(d if isinstance(d, list) else d["doc_ids"])


def run_ids(run_dir) -> set:
    p = pathlib.Path(run_dir)
    if not p.is_absolute():
        p = _ROOT / p
    return {os.path.basename(f)[:-5] for f in glob.glob(str(p / "raw" / "*.json"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gt-dir", default="gt/invoice/fatura")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--per", type=int, default=20)
    ap.add_argument("--mode", choices=("shuffle", "sample"), default="shuffle")
    ap.add_argument("--pilot-size", type=int, default=10)
    ap.add_argument("--rows-key", default="n_gt_rows",
                    help="meta key build_gt.py reads for strata; the live bug is that FATURA "
                         "emits 'gt_line_item_rows'")
    ap.add_argument("--no-pilot-burn", action="store_true",
                    help="replay without the pilot's RNG draw (older code paths)")
    ap.add_argument("--identify", metavar="PLAN",
                    help="search (seed, per, mode, burn) for the combination that reproduces PLAN")
    ap.add_argument("--seeds", default="42,20260901,20260903",
                    help="candidate seeds for --identify")
    ap.add_argument("--against", action="append", default=[], metavar="PLAN",
                    help="diff the drawn plan against a plan file (repeatable)")
    ap.add_argument("--against-run", action="append", default=[], metavar="RUN_DIR",
                    help="diff against the documents a run actually executed (repeatable)")
    ap.add_argument("--nesting", type=int, metavar="K",
                    help="check whether the K-per-cluster draw is a subset of --per")
    ap.add_argument("--write", metavar="OUT", help="write the drawn plan to OUT (refuses to overwrite)")
    ap.add_argument("--slices", type=int, default=0, metavar="N",
                    help="also write N disjoint slices of the drawn plan, sliced by POSITION "
                         "within each cluster so every slice is template-balanced and slices "
                         "1..j are exactly the (j*per/N)-per-cluster draw. Requires --write "
                         "and a --per divisible by N. Only meaningful with --mode shuffle: "
                         "rng.sample redraws for every k, so its plans do not nest.")
    ap.add_argument("--slice-name", default=None, metavar="TEMPLATE",
                    help="filename template for the slices, with {i} for the 1-based slice "
                         "number, e.g. gt/invoice/fatura/main_500_seed42_slice{i}.json")
    a = ap.parse_args()

    gt_dir = pathlib.Path(a.gt_dir)
    if not gt_dir.is_absolute():
        gt_dir = _ROOT / gt_dir
    recs, by_cluster, by_keyset = load_gt(gt_dir)
    sizes = sorted({len(v) for v in by_cluster.values()})
    print(f"ground truth : {len(recs)} docs / {len(by_cluster)} clusters "
          f"/ {len(by_keyset)} key-sets / cluster sizes {sizes}")

    def burner(rng):
        burn_pilot(rng, recs, by_keyset, a.pilot_size, a.rows_key)
    burn = None if a.no_pilot_burn else burner

    if a.identify:
        want = plan_ids(a.identify)
        per_guess = len(want) // len(by_cluster)
        print(f"\nidentifying {a.identify}  ({len(want)} docs -> {per_guess} per cluster)")
        hits = 0
        for seed in [int(s) for s in a.seeds.split(",")]:
            for per in {per_guess, 10, 20, 40}:
                for mode in ("shuffle", "sample"):
                    for b in (burner, None):
                        if set(draw(by_cluster, seed, per, mode, burn=b)) == want:
                            print(f"  REPRODUCED  seed={seed} per={per} mode={mode} "
                                  f"pilot_burn={b is not None}")
                            hits += 1
        if not hits:
            print("  NOT reproduced by any candidate. The plan was hand-edited, built from a "
                  "different ground truth, or built by code no longer present.")
        return 0

    if a.seed is None:
        ap.error("--seed is required unless --identify is given")

    got = draw(by_cluster, a.seed, a.per, a.mode, burn=burn)
    print(f"\ndrawn        : seed={a.seed} per={a.per} mode={a.mode} "
          f"pilot_burn={burn is not None} -> {len(got)} docs")
    S = set(got)

    if a.nesting:
        sub = set(draw(by_cluster, a.seed, a.nesting, a.mode, burn=burn))
        print(f"nesting      : per={a.nesting} subset of per={a.per}? "
              f"{'YES — slicing is safe' if sub <= S else 'NO — the two sizes are different samples'}"
              f"  (overlap {len(sub & S)}/{len(sub)})")

    for p in a.against:
        o = plan_ids(p)
        print(f"vs {p}: n={len(o)} identical={S == o} "
              f"shared={len(S & o)} only-here={len(S - o)} only-there={len(o - S)}")

    for r in a.against_run:
        done = run_ids(r)
        new = S - done
        print(f"vs {r}: executed={len(done)} reusable={len(S & done)} "
              f"NEW={len(new)}  est ${len(new) * COST_PER_DOC_USD:,.2f} "
              f"at ${COST_PER_DOC_USD}/doc")

    if a.slices and not a.write:
        ap.error("--slices requires --write (the slices are children of the written plan)")
    if a.slices:
        if a.per % a.slices:
            ap.error(f"--per {a.per} is not divisible by --slices {a.slices}")
        if a.mode != "shuffle":
            ap.error("--slices only makes sense with --mode shuffle; rng.sample draws a fresh "
                     "combination for every k, so its plans neither nest nor slice")

    if a.write:
        out = pathlib.Path(a.write)
        if not out.is_absolute():
            out = _ROOT / out
        prov = provenance(gt_dir, a.seed, a.per, a.mode, burn is not None,
                          a.pilot_size, a.rows_key)
        index = {r["doc_id"]: r for r in recs}
        picked = draw_by_cluster(by_cluster, a.seed, a.per, a.mode, burn=burn)

        targets = [(out, sorted(got), None)]
        if a.slices:
            size = a.per // a.slices
            template = a.slice_name or (str(out.with_suffix("")) + "_slice{i}.json")
            for k in range(a.slices):
                ids = sorted(d for c in sorted(picked) for d in picked[c][k * size:(k + 1) * size])
                path = pathlib.Path(str(template).format(i=k + 1))
                if not path.is_absolute():
                    path = _ROOT / path
                targets.append((path, ids, (k + 1, size)))

        clash = [t for t, _ids, _s in targets if t.exists()]
        if clash:
            print("!! refusing to overwrite an existing plan:")
            for t in clash:
                print(f"     {t}")
            print("   A plan file is the definition of an experiment. Pick new names, or move "
                  "the old ones aside deliberately.")
            return 2

        sibling_names = [t.name for t, _i, sl in targets if sl]
        for path, ids, sl in targets:
            body = {
                "dataset": gt_dir.name, "doc_type": gt_dir.parent.name,
                "n_docs": len(ids),
                "n_templates": len({index[d]["cluster_id"] for d in ids}),
                "purpose": "the reported numbers",
                **prov,
            }
            if sl is None:
                body["selection"] = (f"{a.per} per cluster, seed {a.seed}, draw mode {a.mode}, "
                                     f"pilot_burn={burn is not None}")
                if a.slices:
                    body["slices"] = sibling_names
                    body["slice_note"] = (
                        f"sliced by position within each cluster into {a.slices} disjoint "
                        f"plans of {a.per // a.slices} per template. Their union is exactly "
                        f"this plan, and slices 1..j are exactly the "
                        f"{a.per // a.slices}*j-per-cluster draw of the same seed -- so a run "
                        f"can stop after any slice and still be a balanced, complete design.")
            else:
                k, size = sl
                body["selection"] = (
                    f"slice {k} of {a.slices}: documents at positions "
                    f"{(k - 1) * size}-{k * size - 1} of each cluster's seed-{a.seed} "
                    f"{a.mode} order, i.e. {size} per template")
                body["slice_index"], body["n_slices"] = k, a.slices
                body["parent_plan"] = str(out.relative_to(_ROOT))
                body["sibling_plans"] = [n for n in sibling_names if n != path.name]
                body["cumulative_note"] = (
                    f"slices 1..{k} together are the {size * k}-per-cluster draw of seed "
                    f"{a.seed}; run them in order and every stopping point is a valid plan.")
            body["doc_ids"] = ids
            path.write_text(json.dumps(body, indent=2), encoding="utf-8")
            per_t = collections.Counter(index[d]["cluster_id"] for d in ids)
            print(f"written      : {path.relative_to(_ROOT)}  {len(ids):>5d} docs / "
                  f"{len(per_t):>3d} templates / "
                  f"{sorted(set(per_t.values()))} per template")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
