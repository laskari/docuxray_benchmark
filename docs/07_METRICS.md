# Step 7 — metrics

**Goal:** numbers that survive scrutiny. **Cost:** nothing, and free to re-run after a fix.

```bash
python steps/step7_metrics.py runs/smoke     # -> results.json + results.md
```

## What is reported

**Per arm:** micro recall with a 95% interval, macro recall (per field, unweighted), recall over
headline-eligible fields only, correct-null rate, hallucination count, and the no-tax slice.

**Per field:** n_docs, n_clusters, recall + interval, exact-after-normalisation, hallucinations,
effective n, and whether it is headline-eligible with the reason if not.

**Per file:** in the step 6 sheet.

**Judge, as an error detector:**

|  | flagged | silent |
|---|---|---|
| wrong in RAW | TP | FN |
| correct in RAW | FP | TN |

plus **fields fixed**, **fields harmed**, net lift, and **harm rate** — the share of fields
correct in B that C got wrong. *A positive net lift with a high harm rate is not a good trade,*
and harm rate is the number that decides whether the stage ships.

## Four rules that make the numbers honest

**Intervals resample clusters, not documents.** 200 renderings of one template are one
observation. Resampling documents would report intervals roughly 14× too narrow.

**Micro and macro both.** Micro is dominated by high-frequency fields; macro lets a field with
n=3 swing the average. Publish both, with support beside every row.

**Headline eligibility is enforced, not advisory.** A field is barred if it has too few distinct
ground-truth values **or** is identical across every instance of every cluster. The second test
is what catches FATURA's seller block — 34–41 distinct values, comfortably above a floor of 20,
but baked into the artwork so all 200 instances are one observation. A distinct-value count
alone would have waved it through.

**Slices that make a field easier are published separately.** 45% of scoreable grand-total values
sit on pages with no tax label, where including-tax and excluding-tax are the same figure and the
distinction cannot be got wrong. All-docs and taxed-only are reported side by side.

## What must never be folded into a headline

* correct-null rate — most fields are null on most documents
* hallucinations — reported as their own count; for accounting software this is the most
  damaging error class and being candid about it is a credibility asset
* fields barred by the eligibility test
* anything from a research-only field (FATURA line items)

## Before publishing

Run the variance probe — the same 50 documents three times — and publish the run-to-run spread.
If variance is ±2% and you are claiming a 1.5-point judge lift, the claim is noise.
