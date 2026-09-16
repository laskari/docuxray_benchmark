# CORD-v2 benchmark results

`runs/cord_test_main_100_verbatim` · metric **accuracy / exact match** · **100 documents, the held-out `test` split** · 251 GT
line-item rows · models gemini-3-flash-preview / gemini-3-flash-preview · cost $7.615 · field
map **1.0-draft** (pending two-reviewer sign-off) · git ai_backend `c831eef+dirty`

**Metric: accuracy under exact match for every key** (`scoring.match_policy: exact`) — no ANLS
thresholds, text and address scored like numbers. Row *pairing* still uses ANLS 0.8, because that
is an alignment device and not a metric.

**Ground truth is VERBATIM.** CORD publishes strings and never says what they are worth, so
ground truth stores each value exactly as downloaded — `"24.000"`, `"Rp 38.000"`, `"@24.000"` —
and no interpretation of ours enters it. These numbers therefore answer one question:

> **does the pipeline transcribe what the receipt prints?**

They do **not** answer whether the shipped *amount* is right. On the 33 test receipts that print
Indonesian dot-thousands, the product's normaliser turns `"24.000"` into `24.0`; because ground
truth is also `"24.000"`, both sides read as 24 and the cell matches. That defect is real and
measured, but it is invisible here by construction. See "What these numbers do not cover".

---

## Headline — arm FINAL (the shipped output)

| | |
|---|--:|
| **scalar totals, accuracy** | **91.44%**  [86.4, 95.9] |
| **line items, row F1** | **90.4%**  (precision 87.1%, recall 94.0%) |
| **line items, cell accuracy** (aligned rows) | **93.6%** |
| line items, whole rows correct | 84.3% |
| line items, whole tables correct | **56.0%** |

222 scalar field instances over 99 documents; 236 of 251 GT rows aligned, 271 emitted.
Intervals are 95% from a bootstrap over documents — on CORD the document is the cluster, so
effective n is 100.

## Line items — arm FINAL

| cell | correct / n | accuracy |
|---|--:|--:|
| `description` | 215 / 236 | 91.1% |
| `quantity` | 205 / 205 | **100.0%** |
| `lineTotal` | 222 / 235 | 94.5% |
| `unitPrice` | 51 / 57 | 89.5% |
| `itemCode` | 10 / 11 | 90.9% |
| `discountPercent` | 2 / 5 | *not reportable* |
| `discountAmount` | 1 / 5 | *not reportable* |

## Scalar totals — arm FINAL

| field | n | recall |
|---|--:|--:|
| `totals.totalIncludingTax` | 95 | 92.6% |
| `totals.change` | 56 | 92.9% |
| `totals.cash` | 65 | 90.8% |
| `totals.discountTotal` | 6 | *not reportable* |

## Keyed totals sections — arm FINAL

| section | GT rows | aligned | row F1 | value accuracy |
|---|--:|--:|--:|--:|
| `totals.otherCharges` | 12 | 12 | **100.0%** | **100.0%** |
| `totals.taxes` | 44 | 39 | 88.6% | 89.7% |
| `totals.subtotal` | 65 | 59 | 87.4% | **96.6%** |
| `totals.taxes[].percentage` | 17 | — | — | **100.0%** |

Rows align on the caption the receipt prints (`SUBTOTAL`, `PB1`, `SVC CHG 6%`), recovered from
`valid_line`. The caption is not itself a scored cell: a row can only align once it has
matched, so scoring it re-reported the alignment decision as a free win.

## All three arms

| arm | scalar accuracy | line-item cells | rows F1 |
|---|--:|--:|--:|
| RAW — extraction, the judge's input | 66.22% | 94.6% | 90.8% |
| RAW_POSTPROCESSED — scoring construct | 65.77% | 94.8% | 90.8% |
| **FINAL — shipped** | **91.44%** | **93.6%** | **90.4%** |

RAW is far lower on the scalars for a structural reason, not a quality one: 23–31% of totals
are still `null` at that stage, and the judge and refinement fill them in. FINAL is the number
to quote for what ships.

## The judge

| | |
|---|--:|
| detector precision | 95.5% |
| detector recall | 84.0% |
| detector F1 | 89.4% |
| fields fixed | 58 |
| fields harmed | 1 |
| net | **+57** |
| harm rate | **0.7%** |

Measured against RAW_POSTPROCESSED, so the deterministic postprocessor's work is not credited
to the judge. Postprocessing alone moved almost nothing — 3 fixed, 4 harmed of 222 fields — so
`get_postprocessor("receipt")` is close to inert on this corpus. The judge is the largest single
contributor to the shipped score, at a very low harm rate.

---

## What these numbers do not cover

* **Whether the shipped amount is right.** Verbatim ground truth measures transcription. The
  parsed corpus (`gt/receipt/cord`) is still built and still scored in
  `runs/cord_test_main_100`, and it is the only thing that evidences the normalisation defect —
  it is simply not reported here. Under the previous threshold scoring the same predictions
  scored **64.9%** scalar against it; that has not been re-derived under exact match, because
  the parsed corpus is no longer reported.
* **The dot-thousands defect.** 28 test receipts print a dot-thousands total; **27 come out
  1000× low**, including 16 where the currency was correctly inferred as IDR. `test-39` is the
  exception and the clue — it printed two dot groups (`3.112.800` → `3112800`), which suggests
  the rule is "one group is a decimal, two are thousands".
* **Every header field** — `parties.*`, `receiptInfo.*`, `currency`. CORD's taxonomy has no
  store-name, address, date or document-number class and the source images are deliberately
  blurred there, so those paths are never annotated and leave every denominator rather than
  scoring as correct nulls. SROIE is the dataset that measures the header.
* **Hallucinations** — structurally zero, not a result. CORD records what an annotator marked,
  so a label's absence is not evidence the fact is absent from the page.
* **Sub-items** (`menu.sub`, 157 documents), voided lines, and CORD's `etc` catch-alls — no home
  in `ReceiptData`; `unmapped` in the field map with a reason each.

## Caveats

1. The field map is **1.0-draft**. Amendments were made from the dev smoke run — all to the
   instrument, never to a threshold — and the test split has seen none of them.
2. `n=100`, so the intervals are wide. `totals.discountTotal` (n=6),
   `lineItems[].discountAmount` and `discountPercent` (n=5) are under the 30-document bar and
   are marked *not reportable* above; do not quote them.
3. Verbatim ground truth inherits CORD's own transcription, including its inconsistencies —
   test-13 records one amount as `3.000` and another as `3,000` on the same receipt. That is
   faithful to the source, and it is scored as printed.
4. Train (800 documents) is built and untouched, held in reserve.
