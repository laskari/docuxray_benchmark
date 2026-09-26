#!/usr/bin/env python3
"""PROBE -- what would `parties.seller.name` score if the printed header text the model sweeps
into it were removed before comparison?

    python scripts/probe_seller_name_scope.py runs/sroie_t626

Three stages, applied in order and reported CUMULATIVELY, so each one's marginal contribution
and its harm are visible separately:

    1  strip a trailing REGISTRATION / GST NUMBER      GARDENIA ... SDN BHD (139386 X)
    2  truncate at the LEGAL FORM                      UNIHAKKA ... SDN BHD BAR WANG RICE@...
    3  drop a BRAND PREFIX                             SUSHI MENTAI - MAHKOTA CHERAS MIZU ...

Stages 1 and 2 are GROUND-TRUTH BLIND: they look only at the model's own string and at
conventions of Malaysian entity names. Stage 3 is NOT, and is labelled an ORACLE wherever it
appears -- see the note above `oracle_drop_prefix`. Read stage 3 as the SIZE OF THE REMAINING
PRIZE, never as a score.

A DIAGNOSTIC, not a scoring change. It writes no results file, amends no map and touches no
published number; it answers one question -- "how much of this field's exact-match failure is
the trailing company registration number, and how much is something else?" -- and prints the
residue so the rest cannot be quietly attributed to the same cause.

WHY A PROBE AND NOT A RULE. Stripping text from a prediction before comparing it is a
post-hoc repair of the model's output, and a benchmark that does that silently stops measuring
the product. If the answer here is large the honest responses are (a) fix the extraction so it
does not emit the id in the name field, or (b) amend the map with a reviewed, closed notation
rule of the kind contract 1.11 already admits -- not to bake this regex into the scorer.

WHAT IS STRIPPED. Only a Malaysian company/GST registration number in TRAILING position:

    GARDENIA BAKERIES (KL) SDN BHD (139386 X)      -> GARDENIA BAKERIES (KL) SDN BHD
    PINGHWAI TRADING SDN BHD (1211036-W)          -> PINGHWAI TRADING SDN BHD
    KEDAI PAPAN YEW CHUAN (0005583085-K)          -> KEDAI PAPAN YEW CHUAN
    99 SPEED MART S/B (519537-X)                  -> 99 SPEED MART S/B

At least FIVE digits are required, so an outlet code the page prints as part of a name
('MCDONALD'S ... DT 36', 'SITE 1066') is never touched, and a leading number ('99 SPEED MART',
'7-ELEVEN') is out of scope because the pattern is anchored to the end.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import doctypes                                                       # noqa: E402
from core.canonical import read_jsonl                                 # noqa: E402
from core.matching import compare, load_rule_registry                 # noqa: E402
from core.metrics import (_gt_dir, flatten_prediction, predicted_value,   # noqa: E402
                          unwrap)
from core.normalize import _Absent, normalise_string                  # noqa: E402

PATH = "parties.seller.name"

#: A registration / GST number in TRAILING position, with or without brackets, with or without
#: a 'CO. REG NO :' caption. >= 5 digits and an optional single check letter.
REG_TAIL = re.compile(
    r"""[\s,;.\-]*                      # separator before the id
        \(?\s*
        (?:(?:CO\.?\s*|COMPANY\s*|GST\s*|REG(?:\.|ISTRATION)?\s*)*(?:NO\.?|REG\.?)\s*[:\-]?\s*)?
        (?:(?<![A-Za-z])[A-Z]{1,3})?    # state prefix GLUED to the digits: (JM0128171-A).
                                        # No separator is allowed between the two, and a
                                        # letter may not follow a letter -- without both
                                        # guards this eats word tails ('RESTORAN WAN SHENG
                                        # 002043319-W' -> 'RESTORAN WAN SH') and preceding
                                        # name tokens ('... sdn bhd 1110644-W' -> '... sdn').
        \d{5,14}                        # the number itself
        \s*[-–—]?\s*
        [A-Z]?                          # optional check letter
        \s*\)?\s*$""",
    re.IGNORECASE | re.VERBOSE)

#: An opening bracket left dangling once its contents were the id and nothing else.
_ORPHAN_OPEN = re.compile(r"[\s,;.\-]*\([^()]{0,3}$")


def strip_registration_id(value):
    """Remove every trailing registration-number group. Returns the value unchanged when the
    result would be empty -- a name that is ONLY an id is a different finding and must not be
    turned into a blank that scores as 'model emitted nothing'."""
    if not isinstance(value, str):
        return value
    out = value
    for _ in range(3):                       # a few pages print the company no. AND the GST no.
        nxt = _ORPHAN_OPEN.sub("", REG_TAIL.sub("", out)).strip(" ,;-")
        if nxt == out or not nxt:
            break
        out = nxt
    return out if out else value


#: The legal form a Malaysian registered entity name ends with. The FIRST occurrence is the
#: cut point, not the last: a page that prints "GERBANG ALAF RESTAURANTS SDN BHD ... FORMERLY
#: KNOWN AS GOLDEN ARCHES RESTAURANTS SDN BHD" carries two, and the name is the first.
LEGAL_FORM = re.compile(
    r"\b(?:SDN\.?\s*BHD\.?|SENDIRIAN\s+BERHAD|BERHAD\b|BHD\.?|S\s*/\s*B\b|PLT\b)", re.IGNORECASE)


def truncate_at_legal_form(value):
    """Stage 2. Cut everything after the entity's legal form.

    GROUND-TRUTH BLIND. It encodes one convention -- a registered Malaysian name ENDS at
    'SDN BHD' / 'BHD' / 'S/B' -- so anything the model appended after it (the outlet, the
    branch, 'LICENSEE OF ...') is not part of the name. A value carrying no legal form is
    returned untouched rather than guessed at, which is why 'THREE STOOGES BISTRO & CAFE'
    (ground truth 'THREE STOOGES') is deliberately left failing: nothing on the string says
    where that name stops.
    """
    if not isinstance(value, str):
        return value
    m = LEGAL_FORM.search(value)
    if not m:
        return value
    out = value[:m.end()].strip(" ,;-")
    return out or value


def oracle_drop_prefix(value, truth):
    """Stage 3 -- AN ORACLE. NOT a scoring rule and never to be made one.

    The remaining shape is a brand or outlet printed BEFORE the registered name
    ("SUSHI MENTAI - MAHKOTA CHERAS MIZU MENTAI SDN. BHD.", "Morganfield's Timeless
    Kitchenette Sdn Bhd"). Unlike a trailing registration number, nothing IN THE STRING marks
    where the prefix ends: the tokens are ordinary words, the separator is often absent, and
    the page's line break -- the one real signal -- is gone by the time the value reaches here.
    Every GT-blind rule tried on it either missed the cases or cut real names short.

    So this consults the ground truth, and therefore measures NOTHING about the model. It
    exists to answer one question honestly: if the prefix problem were solved perfectly
    upstream, how many more documents would pass? That is an upper bound on the prize, and it
    is reported on its own line, never folded into a score.
    """
    if not isinstance(value, str) or not isinstance(truth, str):
        return value
    t = normalise_string(truth, drop_punct=True) or ""
    toks = value.split()
    for i in range(1, len(toks)):                 # shortest prefix dropped that still works
        cand = " ".join(toks[i:])
        if (normalise_string(cand, drop_punct=True) or "") == t:
            return cand
    return value


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--doc-type", default=None)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--arm", default="FINAL")
    a = ap.parse_args()

    run = pathlib.Path(a.run_dir)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    spec = doctypes.get(a.doc_type or manifest.get("doc_type", "receipt"))
    dataset = a.dataset or manifest.get("dataset")
    gt = {r.doc_id: r for r in read_jsonl(str(_gt_dir(spec.name, dataset) / "ground_truth.jsonl"))}
    rule = load_rule_registry(str(_ROOT / "schema" / f"{spec.name}_leaf_paths.tsv"), spec)[PATH]

    STAGES = [("0  as shipped",                      lambda v, t: v),
              ("1  + strip registration id",         lambda v, t: strip_registration_id(v)),
              ("2  + truncate at legal form",        lambda v, t: truncate_at_legal_form(v)),
              ("3  + drop brand prefix  [ORACLE]",   oracle_drop_prefix)]

    arms = ("RAW", "RAW_POSTPROCESSED", "FINAL")
    ok = {arm: [0] * len(STAGES) for arm in arms}
    den = collections.Counter()
    moved = {arm: [[] for _ in STAGES] for arm in arms}     # fixed by THIS stage
    hurt = {arm: [[] for _ in STAGES] for arm in arms}      # broken by THIS stage
    residue = []

    for f in sorted((run / "raw").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if not d.get("ok"):
            continue
        rec = gt.get(d["doc_id"])
        if rec is None or PATH not in rec.annotated_fields:
            continue
        truth = rec.gt.get(PATH)
        if isinstance(truth, _Absent):
            continue
        for arm, payload in (d.get("arms") or {}).items():
            if arm not in arms or not isinstance(payload, dict):
                continue
            den[arm] += 1
            value = predicted_value(PATH, flatten_prediction(unwrap(payload, spec)), spec)
            prev = None
            for si, (_label, fn) in enumerate(STAGES):
                value = fn(value, truth)
                got = compare(value, truth, rule).matched
                ok[arm][si] += got
                if prev is not None:
                    if got and not prev:
                        moved[arm][si].append(d["doc_id"])
                    elif prev and not got:
                        hurt[arm][si].append(d["doc_id"])
                prev = got
            if arm == a.arm and not prev:
                residue.append((d["doc_id"], str(truth), str(value)))

    print(f"PROBE — {PATH}, cumulative scope repair")
    print(f"run {run.name} · {den[a.arm]} scored documents · exact criterion, unchanged")
    print(f"NOTHING here is published: no results file is written and no map is amended.\n")
    print(f"{'stage':36s} " + " ".join(f"{arm:>12s}" for arm in arms) + f"{'fixed':>8s}{'harmed':>8s}")
    for si, (label, _fn) in enumerate(STAGES):
        cells = " ".join(f"{ok[arm][si]/den[arm]:11.1%} " if den[arm] else " " * 12 for arm in arms)
        mv = len(moved[a.arm][si]) if si else 0
        hu = len(hurt[a.arm][si]) if si else 0
        print(f"{label:36s} {cells}{mv:8d}{hu:8d}")
    print(f"\n   'fixed' / 'harmed' are that stage's MARGINAL effect on arm {a.arm}.")

    print(f"\nRESIDUE — {len(residue)} documents still failing on {a.arm} after all three stages:")
    kinds = collections.Counter(); ex = collections.defaultdict(list)
    for doc, t, p_ in residue:
        tn = (normalise_string(t, drop_punct=True) or "").split()
        pn = (normalise_string(p_, drop_punct=True) or "").split()
        if pn[:len(tn)] == tn and len(pn) > len(tn):
            k = "still appends text after the name"
        elif tn[:len(pn)] == pn and len(pn) < len(tn):
            k = "emitted only part of the name"
        elif set(tn) <= set(pn):
            k = "name present but with extra tokens around it"
        else:
            k = "different text — misread, GT typo, or a name with no legal form"
        kinds[k] += 1; ex[k].append(doc)
    for k, v in kinds.most_common():
        print(f"   {v:4d}  {k}   e.g. {ex[k][:6]}")
    for doc, t, p_ in residue[:10]:
        print(f"     {doc}: GT={t!r}")
        print(f"          ->  {p_!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
