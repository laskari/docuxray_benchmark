# Step 6 — comparison for human review

**Goal:** a sheet a person can adjudicate in. **Cost:** nothing.

```bash
python steps/step6_compare.py runs/smoke     # -> runs/smoke/review_smoke.xlsx
```

## Why a sheet and not just metrics

Metrics tell you the rate. They cannot tell you whether a mismatch is the model's fault, the
ground truth's fault, or the comparison rule being too strict. Only a person looking at the
values can, and **that judgement is itself a published result** — the share that comes back
`gt_error` is your ground-truth error rate.

## Layout

**Fields** — one row per (document, field):

| doc_id · template · field · rule · headline | **GT** · **A raw** · **B postproc** · **C refined** | A? · B? · C? · **B→C** | judge flagged · issue · corrected | **ADJUDICATION** · NOTES |

Ground truth sits beside all three arms, with the harness's own verdict for each — you should
never have to decide by eye whether `9.93` matches `(-) 9.93`. Mismatches shade red, `FIXED`
green, `HARMED` orange. Filter `B→C = HARMED` for every field the judge broke.

**By file** — per-document rates. A document failing across many fields is usually one layout
problem, not many field problems.

**By field** — per-field rates, with `headline=no` marked so barred fields cannot drift into a
claim.

Both summaries use live formulas up to 5,000 rows so they respond to filtering; above that they
are computed in Python (a main run is ~26,000 rows) and the Read me sheet says which.

## The adjudication column

For every mismatch, one of:

| verdict | meaning |
|---|---|
| `model_error` | the model got it wrong |
| `gt_error` | the ground truth is wrong |
| `normalisation_error` | both are right, the comparison rule is too strict |
| `mapping_error` | the field map points at the wrong schema path |
| `not_an_error` | anything else — explain in NOTES |

Expect two iterations of the map before the main run. Budget 2–3 hours for 99 documents. This is
the step that cannot be delegated to the machine, because the whole point is a human checking
the machine's work against the page.

## What it caught on the first 10 documents — and the lesson

The sheet showed the judge "reverting" the postprocessor's `9.93` to the printed `(-) 9.93`,
dropping `discountTotal` from 100% to 33% in arm C. It read as a clean production defect.

**It was a harness bug.** That version ran the type postprocessor in arm B, so the judge was fed
normalised numbers. In production the postprocessor runs *after* refinement and the judge only
ever sees `{"originalValue": "(-) 9.93"}` — there is nothing for it to revert.

Two things worth keeping from that:

* The comparison sheet did its job. Values side by side, plus the judge's own issue type and
  proposed correction, is what made the mechanism visible at all. A single accuracy number would
  have shown a net improvement and hidden it.
* A finding that looks clean is not therefore true. Before reporting an arm difference as a
  product defect, confirm the arms match the production stage order.
