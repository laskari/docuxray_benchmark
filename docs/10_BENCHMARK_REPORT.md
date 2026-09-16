# DocuXray extraction benchmark — Invoices and Receipts

**Internal technical evaluation.** Not yet team-accepted; not yet an article.

Prepared 2026-09-08 · harness `docuxray_benchmark` · measured system frozen at
ai_backend `c831eef+dirty`, backend `8812b16`, frontend `343119a` ·
extraction and judge both `gemini-3-flash-preview` · image prep `quality_clean_jpeg`

## The metric

**One metric, one rule: ACCURACY under exact match, for every key.**

```
accuracy = exact matches / fields the dataset states a value for
```

*Exact* means exact **after normalisation** — unicode form, whitespace and case are not what is
being measured, and an `addressStructured` object is merged into one string before comparison
because which component a model puts each piece in is not a correctness question. Beyond that
there is no leniency: no ANLS threshold, no partial credit, and text and address fields are
scored the same way as numbers and dates. Set by `scoring.match_policy: exact` in `config.yaml`.

The denominator is fields the dataset annotates **with a value**. Fields annotated as *absent*
are not in it: getting a null right is reported on its own `correct_null_rate` line, and a value
emitted there is a **hallucination**, also reported separately. Folding correct nulls into the
numerator would let a model raise its score by staying silent.

**One place where a threshold survives, deliberately.** Line-item rows are *paired* by text
similarity (ANLS ≥ 0.8) before their cells are compared. That threshold is an **alignment**
device, not a metric: requiring exact text to pair two rows would leave most rows unmatched and
their cells unscoreable, which measures nothing. Once paired, every cell is scored by exact
match. Row precision/recall/F1 are reported separately from cell accuracy for this reason.

**Cost of the choice (measured).** Against the previous ANLS-threshold scoring, exact match
costs invoices **−0.50pp** (99.53% → 99.03%) and receipt line-item cell accuracy **−2.8pp**
(96.4% → 93.6%). Receipt scalar totals are unchanged, being all numeric. The `exact` column that
used to sit beside the headline is now identical to it by construction.

## How to read this document

Three kinds of statement, always labelled:

* **Measured** — a number produced by the harness from a recorded run. Reproducible from the
  run directory and the fixed seed.
* **Observed** — a pattern visible in the data, stated without a causal claim.
* **Interpreted** — our explanation. Arguable, and flagged so it can be argued with.

Two runs are reported. They are **not** methodologically identical, and the difference is
declared here rather than buried:

| | Invoice | Receipt |
|---|---|---|
| dataset | FATURA (`modified_annotations`) | CORD-v2 (`naver-clova-ix/cord-v2`) |
| run | `runs/s42_main1000` | `runs/cord_test_main_100_verbatim` |
| documents scored | **999** | **100** |
| ground-truth value policy | **parsed** — GT holds the decomposed value | **verbatim** — GT holds the printed string |
| line items in GT | **none** (FATURA annotates no rows) | 251 rows |
| field map | `1.0-frozen` | `1.0-draft` (unsigned) |
| cost | $80.98 | $7.62 |

The value-policy difference is a deliberate per-dataset decision, explained in §4, and it means
**the two headline numbers are not directly comparable**. §8 compares only what is comparable.

---

# PART A — INVOICES (FATURA)

## A1. Dataset and sampling

**Measured.**

* **Dataset** — FATURA, the `modified_annotations` release: 10,000 synthetic invoice images,
  **50 templates × 200 instances**. Annotation is a flat JSON of printed label → printed string
  (`NUMBER`, `DATE`, `TOTAL`, `GST(18%)`, …), 40 distinct labels corpus-wide.
* **Ground truth built for all 10,000 documents** — `gt/invoice/fatura/ground_truth.jsonl`.
  Coverage: 50 clusters, 99 distinct key-sets, **25 scoreable paths**, of which **23 clear the
  headline bar** (≥30 documents and ≥20 distinct GT values, and not constant across clusters).
* **Benchmark subset — 999 documents.** `runs/s42_main1000` is a merge of two recorded runs,
  `s42_slice1` (500) and `s42_slice2` (500). One document was dropped as not-ok, leaving 999
  scored on all three arms with equal denominators (`arm_denominators_equal: true`).
* **Selection** — **seed 42**. Each template's 200 instances are shuffled once with that seed;
  slice 1 takes positions 0–9 of every template, slice 2 positions 10–19. So the subset is
  **20 instances from each of the 50 templates**, balanced by construction.
* **Filtering** — none applied at sampling time. Documents are excluded only per-field, by the
  adapter, with a published reason (§A3).

**Interpreted — why this subset is representative of FATURA.** The corpus's unit of variation is
the *template*: 200 instances of one layout differ in their field values, not in where the
fields sit. Taking an equal number from every template covers 100% of the layout variation and
the full range of key-sets. It is representative **of FATURA**. Whether FATURA represents real
invoices is a separate question, answered honestly in §9.

**Observed — the sampling trap this avoided.** Taking the *first* instance of each template
would have been badly biased: FATURA seeded each template's fixed seller block from its own
Instance0 buyer, so 27 of 31 Instance0 documents carry a duplicate-address quirk that appears
in **zero** of the other 6,169. Position carries meaning in this corpus; the seeded shuffle is
what removes it.

## A2. Pipeline stages

Three arms are scored. Two are durable artifacts in production; the third is a scoring
construct.

| arm | what it is | production equivalent |
|---|---|---|
| **RAW** | extraction + extraction-postprocessing | `pages.$.extraction_postprocessing_result` — exactly what the judge is fed |
| **RAW_PP** | RAW + the type postprocessor | **not a pipeline state.** Production never builds this document |
| **FINAL** | RAW + judge + refinement + postprocessing | `pages.$.postprocessing_result` — the shipped output |

**Why RAW_PP exists.** `NumericValue` sets `extra="forbid"`, so arm RAW carries only
`originalValue` and is scored by the *benchmark's* number parser, while FINAL carries
`normalizedValue` and is scored by the *product's*. RAW → FINAL was therefore never a clean
before/after-judge comparison. RAW_PP applies the same postprocessor FINAL uses, to RAW, so the
comparison isolates the judge. Measured on an earlier 969-document run, **65 of 125 apparent
"judge fixes" were that parser difference alone.**

### Worked example 1 — postprocessing, `totals.discountTotal`

`Template10_Instance110`, GT `30.86`:

```
RAW      {"originalValue": "(-) 30.86"}                            MISMATCH
RAW_PP   {"originalValue": "(-) 30.86", "normalizedValue": 30.86}  match
FINAL    {"originalValue": "(-) 30.86", "normalizedValue": 30.86}  match
```

**Measured.** This single pattern accounts for almost all of postprocessing's value on invoices:
`totals.discountTotal` goes **73.64% → 100.00%** from RAW to RAW_PP. The extractor read the
printed string correctly; only the sign convention needed resolving, and the product's
postprocessor does it. **No model call, no cost.**

### Worked example 2 — the judge, `totals.totalIncludingTax`

`Template17_Instance68`, GT `631.71`:

```
RAW      {"originalValue": "651.14 $", "…"}   MISMATCH   (651.14 — the wrong line was read)
RAW_PP   {"originalValue": "651.14 $", …}     MISMATCH
FINAL    {"originalValue": "631.71 $", …}     match      (judge corrected it)
```

And the same stage getting it wrong — `Template48_Instance24`, GT `504.89`:

```
RAW      {"originalValue": "504.89 $", "normalizedValue": 504.89}   match
RAW_PP   {"originalValue": "504.89 $", "normalizedValue": 504.89}   match
FINAL    {"originalValue": "(9%)",     "normalizedValue": -9.0}     MISMATCH
```

**Observed.** The judge replaced a correct invoice total with a tax percentage. This is the
shape of judge harm on invoices: not small drift, but a confidently wrong substitution.

## A3. Schema and key mapping

**Measured.**

| | count |
|---|--:|
| DocuXray `InvoiceData` scoreable leaf paths | **72** |
| FATURA annotation labels | **40** |
| Labels **mapped** to a schema path | **19** |
| Labels **unmapped** (annotated, no schema home) | **9** |
| Labels **research-only** | **1** |
| Distinct schema paths targeted by the map | **28** |
| Schema paths declared **not annotated** by this dataset | **29** |
| Paths actually scoreable after per-document exclusions | **25** |
| …of which headline-eligible | **23** |

### Key mapping (mapped labels only)

| FATURA label | DocuXray path(s) | rule |
|---|---|---|
| `NUMBER` | `invoiceInfo.documentNumber` | identifier |
| `DATE` | `invoiceInfo.issueDate` (format) + `issueDateISO` (fact) | date_raw / date_iso |
| `DUE_DATE` | `invoiceInfo.dueDate` + `dueDateISO` | date_raw / date_iso |
| `PO_NUMBER` | `invoiceInfo.purchaseOrderNumber` | identifier |
| `SELLER` / `SELLER_ADDRESS` / `SELLER_EMAIL` / `SELLER_SITE` | `parties.seller.*` | text / address / email |
| `BUYER` / `BILL_TO` / `…_ADDRESS` / `…_EMAIL` | `parties.customer.*` | text / address / email |
| `SHIP_TO`, `SHIP_TO_ADDRESS` … | `parties.shipTo.*` | text / address |
| `SUB_TOTAL` | `totals.subtotal` | numeric |
| `DISCOUNT` | `totals.discountTotal` + `discountPercentage` | numeric |
| `TAX`, `GST(n%)`, `VAT` … | `totals.taxName` + `taxAmount` + `taxPercentage` | text / numeric |
| `TOTAL` | `totals.totalIncludingTax` | numeric |
| `CURRENCY` evidence | `currency` | currency |

### Excluded, and why (measured counts, per document-field)

| exclusion | count | reason |
|---|--:|---|
| `currency` | **3,071** | only a bare `$` as evidence — ambiguous between USD/CAD/AUD, not guessed |
| `parties.customer.*` | **800** | `BUYER` and `BILL_TO` both present; the schema has one customer slot |
| `totals.tax*` | **600** | multi-rate tax: 5 tax lines, `InvoiceData.Totals` is scalar (schema limitation) |
| `totals.tax*` | **600** | multi-rate tax: 2 tax lines, same limitation |
| `invoiceInfo.*` | **5** | known GT defects (a `DATE` holding a due date; a `TOTAL` holding `DUE_AMOUNT`) |

**Dataset-only keys** (FATURA annotates, DocuXray has no field): 9 labels, including the
seller's website/`SELLER_SITE` and layout-only tokens. **DocuXray-only keys**: 44 of the 72
leaves are never annotated by FATURA — most notably **every line-item path**, since FATURA has
**no line-item annotation at all**.

**This is the single largest gap in the invoice benchmark: invoice line-item extraction is not
measured.** `n_gt_line_item_rows: 0`.

## A4. Evaluation methodology

Comparison is **exact match after normalisation, for every key**. What differs per key is only
*how the two sides are normalised before being compared* — never how strict the comparison is.
Normalisation is derived from the schema's declared type, not the field name, so a schema change
gets a rule automatically.

| rule | applies to | comparison |
|---|---|---|
| `identifier` | document/PO numbers | strict-normalised exact. A near miss is a full miss |
| `numeric` | money, percentages, quantities | parse both sides to 2dp Decimal, exact. No tolerance |
| `date_iso` | `*ISO` paths | parse both to ISO-8601, exact — scored as **the fact** |
| `date_raw` | `issueDate`, `dueDate` | format fidelity only — **never in the same denominator as ISO** |
| `text` | names, memos | NFKC + whitespace collapse + case-fold, then **exact** |
| `address` | `addressStructured` | five components merged into one string, punctuation dropped both sides, then **exact** |
| `phone` / `email` | contact fields | digits-only / case-normalised exact |
| `currency` | `currency` | exact; an ambiguous symbol is excluded, not guessed |

**Three-state null semantics** are structural, not conventional:

1. annotated **with a value** → counts toward accuracy;
2. annotated **as absent** → a model emission here is a **hallucination**; never in a headline;
3. **not annotated** → excluded from *every* denominator.

**Interpreted — why exact match everywhere.** These are fields that get posted to a ledger. A
total off by a cent is wrong, not partially right; so is an address missing its postcode. A
single rule also makes one number mean one thing: under the previous scoring, 99.5% meant
"exact" for numbers and "within ANLS 0.8" for addresses, and a reader had to know which key they
were looking at to know what they were being told. The cost is measured and stated above.

**Observed — what exact match reveals.** The three address fields drop the most: `shipTo`
100.00% → 92.18%, `seller` 99.02% → 95.85%, `customer` 99.73% → 96.08%. Those 71 documents were
passing on a merged block that was close but not identical — typically a missing postcode or a
country rendered `US` vs `USA`. Whether that should count is now an explicit policy choice
rather than a threshold buried in the comparison code.

## A5. Results — invoices

**Measured.** 999 documents · 50 clusters · **13,676 scored field instances** · equal
denominators across arms. Intervals are 95% from a bootstrap resampling **clusters** (templates),
not documents — 200 instances of one layout are one layout observation, so effective n is 50.

| | RAW | RAW_PP | FINAL |
|---|--:|--:|--:|
| **accuracy (micro)** | 98.33% [97.9, 98.8] | 98.79% [98.3, 99.2] | **99.03%** [98.6, 99.3] |
| accuracy (macro, per field unweighted) | 97.76% | 98.82% | 98.92% |
| headline-eligible fields only | 98.22% | 98.72% | 99.00% |
| correct-null rate *(separate, never a headline)* | 91.59% | 91.59% | 91.40% |
| hallucinations *(separate)* | 89 | 89 | **91** |

Stage deltas: **RAW → RAW_PP +0.46pp**, **RAW_PP → FINAL +0.24pp**, total **+0.70pp**.

### Key-level accuracy (all arms)

| path | rule | n | RAW | RAW_PP | FINAL | Δ PP | Δ judge | headline |
|---|---|--:|--:|--:|--:|--:|--:|:--:|
| `currency` | currency | 612 | 100.00 | 100.00 | **100.00** | — | — | **no** |
| `invoiceInfo.issueDate` | date_raw | 979 | 100.00 | 100.00 | **100.00** | — | — | yes |
| `invoiceInfo.issueDateISO` | date_iso | 979 | 100.00 | 100.00 | **100.00** | — | — | yes |
| `invoiceInfo.purchaseOrderNumber` | identifier | 140 | 100.00 | 100.00 | **100.00** | — | — | yes |
| `parties.shipTo.name` | text | 179 | 100.00 | 100.00 | **100.00** | — | — | yes |
| `totals.discountPercentage` | numeric | 239 | 100.00 | 100.00 | **100.00** | — | — | yes |
| `totals.discountTotal` | numeric | 239 | 73.64 | 100.00 | **100.00** | **+26.36** | — | yes |
| `totals.subtotal` | numeric | 680 | 98.97 | 98.97 | **100.00** | — | +1.03 | yes |
| `totals.taxAmount` | numeric | 439 | 99.77 | 99.77 | **100.00** | — | +0.23 | yes |
| `totals.taxPercentage` | numeric | 439 | 99.77 | 99.77 | **100.00** | — | +0.23 | yes |
| `invoiceInfo.dueDate` | date_raw | 579 | 100.00 | 100.00 | 99.83 | — | −0.17 | yes |
| `invoiceInfo.dueDateISO` | date_iso | 579 | 100.00 | 100.00 | 99.83 | — | −0.17 | yes |
| `invoiceInfo.documentNumber` | identifier | 879 | 99.66 | 99.66 | 99.77 | — | +0.11 | yes |
| `totals.totalIncludingTax` | numeric | 839 | 98.21 | 98.21 | 99.76 | — | +1.55 | yes |
| `parties.customer.name` | text | 740 | 99.59 | 99.59 | 99.73 | — | +0.14 | yes |
| `parties.customer.phone` | phone | 740 | 99.19 | 99.19 | 99.46 | — | +0.27 | yes |
| `parties.shipTo.phone` | phone | 179 | 99.44 | 99.44 | 99.44 | — | — | yes |
| `parties.seller.name` | text | 999 | 99.41 | 99.41 | 99.26 | — | −0.15 | yes |
| `totals.taxName` | text | 439 | 99.32 | 99.32 | 98.63 | — | −0.68 | **no** |
| `parties.seller.email` | email | 999 | 96.13 | 96.13 | **97.95** | — | +1.82 | yes |
| `parties.shipTo.email` | email | 179 | 98.32 | 98.32 | 97.77 | — | −0.56 | yes |
| `parties.customer.email` | email | 740 | 96.89 | 96.89 | 97.57 | — | +0.68 | yes |
| `parties.customer.addressStructured` | address | 740 | 96.49 | 96.49 | 96.08 | — | −0.41 | yes |
| `parties.seller.addressStructured` | address | 999 | 94.88 | 94.88 | 95.85 | — | +0.98 | yes |
| `parties.shipTo.addressStructured` | address | 179 | 94.41 | 94.41 | **92.18** | — | −2.23 | yes |

Six paths at 100.00% in FINAL. `currency` and `totals.taxName` are barred from headlines by the
eligibility test (too few distinct values / constant across clusters), not by hand.

### Document-level performance

**Measured.** Not reported for invoices. FATURA annotates no line items, so there is no
table-exact or row-level figure, and a whole-document "all fields correct" rate over 25 paths
with per-document exclusions would compare different field sets across documents. The
per-field table above with `n` stated per path is the honest unit here.

## A6. Error and key-level analysis — invoices

**Best-performing keys (measured).** Six paths at 100.00% in FINAL: `issueDate`,
`issueDateISO`, `purchaseOrderNumber`, `shipTo.name`, `discountPercentage`, `discountTotal` —
plus `subtotal`, `taxAmount` and `taxPercentage` also reaching 100.00% after the judge.

**Worst-performing keys (measured), FINAL:**

| path | accuracy | states |
|---|--:|---|
| `parties.shipTo.addressStructured` | **92.18%** | 165 correct, **14 wrong** |
| `parties.seller.addressStructured` | 95.85% | 786 correct, 28 wrong, 6 missing, 178 correct-null, **1 hallucination** |
| `parties.customer.addressStructured` | 96.08% | 711 correct, **29 wrong** |
| `parties.customer.email` | 97.57% | 722 correct, 18 wrong |
| `parties.shipTo.email` | 97.77% | 175 correct, 4 wrong |
| `parties.seller.email` | 97.95% | 430 correct, 1 wrong, 8 missing, 560 correct-null |
| `totals.taxName` | 98.63% | 433 correct, 6 wrong |

**Observed.** Under exact match the three **address** fields are now the worst keys on invoices,
displacing email. They were the fields the ANLS threshold was flattering most.

**The largest single defect is not in the accuracy column.** `parties.seller.name` shows
**90 hallucinations** out of 999 documents (states: 675 correct, 229 correct-null, 5 wrong,
**90 hallucination**). FATURA licenses authoritative absence for this label, so this is a real
measurement: **on ~9% of invoices the pipeline emits a seller name where the page carries none.**

**Observed causes, with examples:**

* **Judge substitution errors** — `totals.taxName` regresses −0.68pp because the judge rewrites
  `GST` to `GST(7%)` and `GST(18%)`, folding the rate into the name. Two documents shown in §A2.
  `invoiceInfo.dueDate` regresses because the judge changed `18-Mar-1998` to `18-Mar-2006`.
* **Formatting, not reading** — `totals.discountTotal` at 73.64% in RAW is entirely
  `"(-) 30.86"` vs `30.86`. The extractor read it correctly; the sign convention is a
  postprocessing concern, and postprocessing fixes 100% of it.
* **Missing rather than wrong** — `parties.seller.email` and `subtotal` failures in RAW are
  predominantly `null`, filled in later by the judge (§A7). Extraction under-emits; it does not
  mis-read.
* **Annotation limits, not model limits** — 3,071 `currency` and 1,200 tax-field exclusions are
  the *dataset* and *schema* refusing to answer, not the model failing. They are excluded from
  every denominator, so they neither help nor hurt the score.

## A7. AI Judge value — invoices

**Measured.** Detector matrix scored against **RAW** (what the judge was shown); fixed/harmed
against **RAW_PP** (so the postprocessor's work is not credited to the judge).

| | flagged | silent |
|---|--:|--:|
| **wrong in RAW** | 116 | 201 |
| **correct in RAW** | **1,064** | 13,353 |

Matrix total 14,734 — counted on arm **payload leaves**, not on the 13,676 scored field
instances, because a leaf the judge saw is not always a leaf the dataset annotates.

| | |
|---|--:|
| detector precision | **9.83%** |
| detector recall | 36.59% |
| detector F1 | 15.53% |
| fields fixed | 89 |
| fields incorrectly changed | 58 |
| fields unchanged | 14,587 |
| **net improvement** | **+31 fields** (+0.24pp) |
| harm rate | 0.40% |

**Keys benefiting most:** `parties.seller.email` (+1.82pp), `totals.totalIncludingTax`
(+1.55pp), `totals.subtotal` (+1.03pp), `parties.seller.addressStructured` (+0.98pp).
**Keys harmed:** `parties.shipTo.addressStructured` (**−2.23pp**), `totals.taxName` (−0.68pp),
`parties.shipTo.email` (−0.56pp), `parties.customer.addressStructured` (−0.41pp), both
`dueDate` paths (−0.17pp), `parties.seller.name` (−0.15pp).

**Interpreted — does the judge provide meaningful value on invoices? Marginally, and it is the
weakest link in the pipeline.** It raises accuracy by **0.24 percentage points**, 89 fixes
against 58 harms. Its detector precision is **9.83%**: it flags 1,180 fields and is right about
116 of them. That costs a model call per section on every document — roughly half the run's
$80.98. The pipeline scores 98.79% without it and 99.03% with it, and its harms are
qualitatively worse than the misses it fixes (a correct total replaced by `(9%)`).

**Observed — exact match made the judge look slightly worse, not better.** Under the previous
threshold scoring it was +35 net on 72 fixes and 37 harms; under exact match it is +31 net on 89
fixes and 58 harms. More fields are now in play at both ends, and the harm side grew faster.

**Caveat.** This is measured where RAW is already 98.33%: only 317 wrong fields in 13,676 exist
for the judge to find. A judge with 10% precision on a nearly-clean input is not necessarily a
judge with 10% precision on a messy one — and §B7 shows exactly that.

---

# PART B — RECEIPTS (CORD-v2)

## B1. Dataset and sampling

**Measured.**

* **Dataset** — CORD-v2 (`clovaai/cord`, released as `naver-clova-ix/cord-v2`), **1,000
  Indonesian receipt photographs**, split **train 800 / dev 100 / test 100**. Annotation is
  word-level: each `valid_line` entry carries `words[] {quad, is_key, row_id, text}`, a
  `category` from a **30-label taxonomy**, a `group_id` and (new in v2) a `sub_group_id`. CORD
  also publishes `gt_parse`, its own nested `{menu:[…], sub_total:{…}, total:{…}}` form.
* **Materialised** by `scripts/prepare_cord_v2.py` to `<split>/{images,annotations}`. All 1,000
  documents carry `gt_parse_source: "cord"` — **nothing was reconstructed by us** — and all
  1,000 images resolve.
* **Ground truth built for all 1,000 documents**: 1,000 clusters, 287 key-sets,
  **2,569 line-item rows**, **8 scoreable paths, all 8 headline-eligible**.
* **Benchmark subset — the entire `test` split, 100 documents, 251 GT rows.** No sampling:
  `"every document in the test split — no sampling, so there is no sampling variance to argue
  about"`. Plan seed `20260907` is recorded but unused at this size.
* **Why v2 and not v1** — v2 corrected mislabels present in v1 and is the only release carrying
  `sub_group_id` and the dev/test splits. Without the splits there is no held-out measurement.

**Interpreted — why the test split is the right subset.** The map was piloted on **dev** and the
numbers come from **test**, so no mapping decision was made on the documents the score is
computed from. Train (800 documents) is built and untouched, held in reserve. Reporting the
whole split rather than a sample removes sampling variance entirely.

**Observed — the corpus is not uniform.** 431 of 1,000 receipts have a single line item and the
largest has 22; 224 have sub-items; 39 have two items sharing a printed name. The test split is
44 small / 39 single / 14 medium / 3 large tables.

## B2. Pipeline stages

Arms are defined identically to §A2.

### Worked example 1 — the judge filling a null, `totals.cash`

`test-10`, GT `20,000`:

```
RAW      {"originalValue": null}                                  MISMATCH  (not emitted)
RAW_PP   {"originalValue": null, "normalizedValue": null}          MISMATCH
FINAL    {"originalValue": "20,000", "normalizedValue": 20000.0}   match
```

**Measured.** This is the dominant transition on receipts: **23–31% of scalar totals are `null`
in RAW and 1–2% in FINAL.** The judge and refinement are doing the recall work.

### Worked example 2 — the judge damaging a correct value, `totals.cash`

`test-57`, GT `42,000`:

```
RAW      {"originalValue": "42,000", …}                            match
RAW_PP   {"originalValue": "42,000", "normalizedValue": 42000.0}   match
FINAL    {"originalValue": ".42,000", "normalizedValue": 42.0}     MISMATCH
```

**Observed.** A leading `.` was introduced, turning 42,000 into 42. One of only 1 harmed field
in the whole receipt run.

## B3. Schema and key mapping

**Measured.**

| | count |
|---|--:|
| DocuXray `ReceiptData` scoreable leaf paths | **49** |
| CORD-v2 annotation labels | **26** |
| Labels **mapped** | **15** |
| Labels **unmapped** (annotated, no schema home) | **11** |
| Distinct schema paths targeted | **19** |
| Schema paths declared **not annotated** | **30** |
| Paths scoreable | **8** (4 scalar + 4 repeated sections) |

### Key mapping

| CORD-v2 label | DocuXray path(s) | rule |
|---|---|---|
| `menu.nm` | `lineItems[].description` | text (ANLS 0.8) — also the row alignment key |
| `menu.num` | `lineItems[].itemCode` | identifier |
| `menu.cnt` | `lineItems[].quantity` | numeric |
| `menu.unitprice` | `lineItems[].unitPrice` | numeric |
| `menu.price` (else `menu.itemsubtotal`) | `lineItems[].lineTotalIncludingTax` ∪ `…ExcludingTax` | numeric, **union** |
| `menu.discountprice` | `lineItems[].discountAmount` or `…discountPercent` | numeric, chosen per value by `%` |
| `sub_total.subtotal_price` | `totals.subtotal[]` rows | numeric, keyed by printed caption |
| `sub_total.tax_price` | `totals.taxes[]` rows + `[].percentage` | numeric |
| `sub_total.service_price`, `…othersvc_price` | `totals.otherCharges[]` rows | numeric |
| `sub_total.discount_price` | `totals.discountTotal` | numeric, positive magnitude |
| `total.total_price` | `totals.totalIncludingTax` | numeric |
| `total.cashprice` / `total.changeprice` | `totals.cash` / `totals.change` | numeric |

**Dataset-only keys (11 unmapped, measured document counts):** `menu.sub` (157 docs — sub-items;
`ReceiptData.lineItems` is flat), `total.menuqty_cnt` (283), `total.creditcardprice` (151),
`sub_total.etc` (76), `total.menutype_cnt` (52), `total.emoneyprice` (51), `total.total_etc`
(33), `menu.vatyn` (3 — free text, not a code), `menu.etc` (2), `void_menu.nm`/`.price` (1).

**DocuXray-only keys:** 30 paths, comprising **every `parties.*` and `receiptInfo.*` field**.
CORD's taxonomy has no store-name, address, phone, date or document-number class **and the
source images are deliberately blurred in the header and footer** (verified on `test/images/13`,
`26`, `32`). Those paths are therefore never annotated and leave every denominator.

**This is the largest gap in the receipt benchmark: receipt header extraction is not measured
at all.** SROIE is the dataset intended to cover it; it is not yet run (§8).

### Excluded, and why (measured, per document-field)

| exclusion | count |
|---|--:|
| GT value present but ambiguous — CORD annotated the same category twice | 25 |
| rows indistinguishable to the aligner, differing only in a discount cell | 24 |
| row has no description or itemCode to align on | 8 |
| the same amount printed again under a different caption (a restatement) | 8 |
| document has no alignable menu rows | 6 |

## B4. Evaluation methodology — receipts

Rules are as §A4, with two receipt-specific decisions.

**1. Ground truth is VERBATIM.** CORD publishes strings and never states their value, so GT
stores each value exactly as downloaded — `"24.000"`, `"Rp 38.000"`, `"@24.000"`. These results
therefore answer *"does the pipeline transcribe what the receipt prints?"* and **not** *"is the
shipped amount right?"*

The reason this matters is measured: on the 28 test receipts printing a dot-thousands total,
**27 come out 1000× low** in the shipped output, because the product reads `"24.000"` as `24.0`.
Under verbatim GT both sides read as 24 and the cell **matches**. The defect is real and is
recorded in §B6; it is invisible in these numbers by construction.

For the record, the same 100 predictions scored against the **parsed** corpus
(`runs/cord_test_main_100`) give **64.9%** scalar micro and **88.3%** line-item cell accuracy.

**2. Repeated sections are row-aligned, never index-wise.** `core/rows.py` performs exact
assignment on identifying text (ANLS ≥ 0.8), breaking ties on numeric agreement. Cells are
scored **only on matched rows and only where GT states a value** — a corpus-wide denominator
per column would be wrong wherever a column is sparse.

* `lineItems` aligns on `description`, then `itemCode`.
* Keyed totals align on the **printed caption**, recovered from `valid_line`'s `is_key` words.
  The caption is **not** a scored cell: a row can only align once it has matched, so scoring it
  would re-report the alignment decision as a free win (it measured 100.0% on every section).
* Where a caption ends in a rate, both the split and whole forms are offered as alignment keys
  (`key`, `keyAlt`), because models disagree about which belongs in the label.
* **Multiple subtotals are never summed.** 9 documents print more than one; against the printed
  grand total the SUM matched in **0 of 8** and the LARGEST in **7 of 8**, because 7 of the 9
  print the same amount twice. One row per distinct amount; exact duplicates are deduplicated
  with a published reason.

**Absence is not authoritative.** CORD records what an annotator marked, so a missing label is
not evidence the fact is absent. Every mapped path is scored for accuracy only and the
**hallucination count is structurally zero — not a result**.

## B5. Results — receipts

**Measured.** 100 documents · 100 clusters · **222 scalar field instances** over 99 documents ·
**251 GT line-item rows** · equal denominators across arms. Intervals bootstrap over documents,
because on a natural corpus the document *is* the cluster, so effective n is 100.

| | RAW | RAW_PP | FINAL |
|---|--:|--:|--:|
| **scalar accuracy (micro)** | 66.22% [57.1, 75.2] | 65.77% [56.5, 74.6] | **91.44%** [86.4, 95.9] |
| accuracy (macro) | 54.22% | 54.02% | 85.73% |
| hallucinations | 0 | 0 | 0 *(structural, not a result)* |
| **lineItems cell accuracy** | 94.6% | 94.8% | **93.6%** |
| lineItems row F1 *(alignment, not the metric)* | 90.8% | 90.8% | 90.4% |

Stage deltas: **RAW → RAW_PP −0.45pp** (a regression), **RAW_PP → FINAL +25.67pp**.

### Key-level accuracy — scalar totals

| path | n | RAW | RAW_PP | FINAL | Δ judge |
|---|--:|--:|--:|--:|--:|
| `totals.change` | 56 | 62.50 | 64.29 | **92.86** | **+28.57** |
| `totals.totalIncludingTax` | 95 | 71.58 | 70.53 | **92.63** | **+22.11** |
| `totals.cash` | 65 | 66.15 | 64.62 | **90.77** | **+26.15** |
| `totals.discountTotal` | 6 | 16.67 | 16.67 | 66.67 | +50.00 |

`totals.discountTotal` at n=6 is under the 30-document bar and **must not be quoted**.

### Key-level accuracy — repeated sections, FINAL

| cell | n | accuracy |
|---|--:|--:|
| `lineItems[].quantity` | 205 | **100.0%** |
| `totals.otherCharges[].value` | 12 | **100.0%** |
| `totals.taxes[].percentage` | 17 | **100.0%** |
| `totals.subtotal[].value` | 59 | 96.6% |
| `lineItems[].lineTotal` | 235 | 94.5% |
| `lineItems[].description` | 236 | **91.1%** |
| `lineItems[].itemCode` | 11 | 90.9% |
| `totals.taxes[].value` | 39 | 89.7% |
| `lineItems[].unitPrice` | 57 | 89.5% |
| `lineItems[].discountPercent` | 5 | 40.0% *(not reportable)* |
| `lineItems[].discountAmount` | 5 | 20.0% *(not reportable)* |

**Observed.** `lineItems[].description` was 100.0% under ANLS ≥ 0.8 and is **91.1%** under exact
match — 21 of 236 item names differ from the printed form in some way (punctuation, spacing,
abbreviation). That is the single biggest effect of the metric change anywhere in this report.

### Document-level performance, FINAL

| | |
|---|--:|
| rows aligned | 236 of 251 GT (271 emitted) — precision 87.1%, recall 94.0% |
| whole rows correct (every stated cell exact) | **84.3%** |
| **whole tables correct** (no row missed, none invented, every cell exact) | **56.0%** |
| `totals.subtotal` row F1 | 87.4% |
| `totals.taxes` row F1 | 88.6% |
| `totals.otherCharges` row F1 | **100.0%** |

`table_exact` at **56.0%** is the number that cannot be gamed by omission, and the honest
document-level figure for receipts. It was 69.0% under threshold scoring.

## B6. Error and key-level analysis — receipts

**Best-performing keys (measured).** `lineItems[].quantity` 205/205 — **perfect on every
aligned row**. `totals.otherCharges[].value` 12/12 and `totals.taxes[].percentage` 17/17 also
100%.

**Worst-performing (measured, excluding the three unreportable):**

| path | FINAL | failure states |
|---|--:|---|
| `lineItems[].unitPrice` | 89.5% | 6 of 57 wrong |
| `totals.taxes[].value` | 89.7% | 4 of 39 wrong |
| `totals.cash` | 90.8% | 59 correct, 5 wrong, 1 missing |
| `lineItems[].description` | **91.1%** | 21 of 236 not exact |
| `totals.totalIncludingTax` | 92.6% | 88 correct, 6 wrong, 1 missing |
| `lineItems[].lineTotal` | 94.5% | 13 of 235 wrong |

**Observed causes, with examples:**

* **A single locale defect dominates the *amount* question.** 28 test receipts print a
  dot-thousands total; **27 come out 1000× low**. `test-32`: the receipt prints
  `GRANDTOTAL 24.000`, `CASH 50.000`, `CHANGED 26.000`; the extractor transcribes them exactly;
  the product's `normalizedValue` is `24.0`, `50.0`, `26.0`. **16 of those 27 had the currency
  correctly inferred as `IDR`**, so the parser does not consult it. `test-39` is the exception
  and the clue — `"3.112.800"` → `3112800`, correct — which suggests the rule is *"one dot group
  is a decimal, two are thousands"*. **This is invisible in the verbatim numbers above.**
* **Extraction under-emits; it does not mis-read.** In RAW, 20 of 65 `cash` and 17 of 56
  `change` values are `null` rather than wrong. §B7 shows the judge recovering them.
* **Annotation inconsistency inherited by verbatim GT.** `test-13` records one amount as
  `3.000` and another as `3,000` on the same receipt. Faithful to CORD, scored as printed.
* **Row alignment is not the weak point.** Row F1 90.4% with `quantity` exact on every aligned
  row. 15 of 251 rows never aligned; 24 discount cells were excluded because two rows were
  indistinguishable to the aligner.
* **Item names are close but not identical.** `description` falls from 100.0% (ANLS ≥ 0.8) to
  **91.1%** (exact) — 21 of 236. These rows still *align*, so the failure is transcription
  detail, not row identification.
* **A judge artefact the verbatim policy creates.** `test-13`: GT `'51.300'`, RAW emitted
  `'51,300'` (comma) → mismatch, FINAL emitted `'51.300'` → match, so the judge is **credited
  for a change that in the parsed corpus is a 1000× error**. Measured, and an argument against
  reading the verbatim column alone.

## B7. AI Judge value — receipts

**Measured.** Detector against RAW; fixed/harmed against RAW_PP.

| | flagged | silent |
|---|--:|--:|
| **wrong in RAW** | 63 | 12 |
| **correct in RAW** | 3 | 144 |

| | |
|---|--:|
| detector precision | **95.45%** |
| detector recall | **84.00%** |
| detector F1 | **89.36%** |
| fields fixed | 58 |
| fields incorrectly changed | **1** |
| fields unchanged | 163 |
| **net improvement** | **+57 fields** (+25.67pp) |
| harm rate | **0.68%** |

**Keys benefiting most:** `totals.discountTotal` (+50.0pp, n=6), `totals.change` (+28.57pp),
`totals.cash` (+26.15pp), `totals.totalIncludingTax` (+22.11pp). **Keys harmed:** one `cash`
value on `test-57`.

**Interpreted — on receipts the judge is the single most valuable stage in the pipeline.** It
takes scalar accuracy from 65.8% to 91.4% at a 0.68% harm rate, with detector precision of 95%.
Without it the receipt pipeline would be unusable for totals. Its numbers are **unaffected** by
the exact-match change, because every receipt scalar is numeric.

## B8. Post-processing value — receipts

**Measured.** 222 fields compared, **3 fixed, 4 harmed, net −1**, harm rate 2.72%; 6 leaves
nulled across 3 documents (`lineItems[].unitPrice.originalValue` ×2,
`lineItems[].lineTotalExcludingTax…`). The three fixes are all the same pattern — `"10, 000"`
with a stray space, which the product's parser handles and the benchmark's does not.

**Interpreted.** `get_postprocessor("receipt")` is **effectively inert on this corpus, and
slightly net-negative**. Contrast invoices, where the same stage fixes 63 fields and harms 0.
The receipt postprocessor has not been given the receipt-specific rules the invoice one has —
most obviously a thousands-separator rule.

---

# PART C — CONCLUSIONS, COMPARISON, AND ANTICIPATED QUESTIONS

## 8. Conclusions and observations

### Invoices

* **Extraction quality (measured).** **99.03% accuracy** on 13,676 field instances, FINAL, under
  exact match for every key. Six paths at 100%. This is a **synthetic corpus with clean, uniform
  rendering**; read it as an upper bound, not as production performance.
* **Post-processing (measured).** +0.46pp, 63 fixed, **0 harmed**, no model call, no cost.
  Almost entirely one pattern: `"(-) 30.86"` → `30.86` on `discountTotal`. Excellent value.
* **AI Judge (measured, interpreted).** +0.24pp net. Detector precision **9.83%** — 1,180 flags,
  116 correct. 89 fixes against 58 harms. **The weakest stage.** Its harms are qualitatively
  severe (a correct total replaced by `(9%)`) and its worst single key regression is
  `shipTo.addressStructured` at −2.23pp.
* **Major weak areas (measured).** **Address fields (92–96%)** — the worst keys once the ANLS
  threshold is removed; email fields (97.6–98.0%); `parties.seller.name` emits **90
  hallucinations in 999 documents**; no line-item measurement at all.

### Receipts

* **Extraction quality (measured).** `quantity` exact on every aligned row; line-item cell
  accuracy **93.6%**; scalar accuracy **91.44%** in FINAL. Whole-table exact **56.0%**, and
  `description` **91.1%** under exact match.
* **Post-processing (measured).** **Net −1 field.** Inert to slightly harmful.
* **AI Judge (measured).** +25.67pp, detector F1 89.4%, one harmed field. **The most valuable
  stage.**
* **Major weak areas (measured).** A single normalisation defect makes **27 of 28**
  dot-thousands receipts ship a 1000×-low amount, worth roughly 26pp of the amount-level score.
  Receipt header extraction is **not measured at all**.

### Key lessons

1. **Interpreted.** The judge's value is inversely related to extraction quality. On invoices
   (RAW 98.33%) it adds 0.24pp at 9.8% precision; on receipts (RAW 65.8%) it adds 25.7pp at 95%
   precision. A single "is the judge worth it?" answer does not exist — it is per-corpus.
2. **Measured.** Post-processing is the cheapest win available and is well-developed for
   invoices and absent for receipts.
3. **Interpreted.** The largest receipt defect is not a reading failure but a **locale
   normalisation** failure, and no amount of extraction improvement fixes it.
4. **Observed.** The benchmark repeatedly caught its *own* instrument errors before they became
   conclusions — invented alignment keys, a tautological scored cell, a summing hypothesis
   refuted by 9 documents. Every one of those would have been published as a model failure.

### Recommendations

| # | recommendation | evidence |
|--:|---|---|
| 1 | Fix dot-thousands normalisation in `normalizedValue`; do not gate on currency inference | 27 of 28 receipts, 16 with IDR correctly inferred |
| 2 | Give the receipt postprocessor the rules the invoice one has | receipts net −1 vs invoices +63 fixed / 0 harmed |
| 3 | Constrain the judge on high-confidence fields, or gate it on extraction confidence | invoice detector precision 9.83%, 58 harms |
| 4 | Investigate `parties.seller.name` hallucination | 90 of 999 invoices |
| 5 | Run SROIE to measure the receipt header | 0 of 30 header paths currently measured |
| 6 | Find an invoice dataset with line-item annotation | FATURA `n_gt_line_item_rows: 0` |

### Invoice vs receipt — short comparison

**Comparable (both parsed-value, like-for-like):** nothing. The corpora use different value
policies by design.

**Comparable structurally:**

| | Invoice (FATURA) | Receipt (CORD-v2) |
|---|--:|--:|
| **accuracy, FINAL** | **99.03%** | **91.44%** scalar / **93.6%** line-item cells |
| documents | 999 | 100 |
| scored field instances | 13,676 | 222 + 754 line-item cells |
| RAW → FINAL, scalar accuracy | +0.70pp | +25.22pp |
| post-processing | **+63 fixed / 0 harmed** | 3 fixed / 4 harmed |
| judge net | +31 fields (+0.24pp) | **+57 fields (+25.67pp)** |
| judge detector precision | **9.83%** | **95.45%** |
| line items measured | **no** | yes |
| header measured | yes | **no** |
| hallucination measurable | yes (89–91 found) | no (structural zero) |

**Interpreted.** The two benchmarks are complementary rather than comparable: invoices measure
the header and totals on clean synthetic renderings with no line items; receipts measure line
items and totals on real photographs with no header. Neither alone characterises the pipeline,
and a published article should say so rather than average them.

## 9. Anticipated questions

Format: **Question → Short answer → Evidence → Caveat.**

**Q1. Why these datasets?**
Both are public, annotated, and standard for their document type; FATURA is the only large
invoice corpus with per-field annotations we can map to the schema, and CORD-v2 is the reference
receipt corpus. *Evidence:* FATURA 10,000 docs / 40 labels; CORD-v2 1,000 docs / 30-label
taxonomy with word-level boxes. *Caveat:* FATURA is **synthetic** and CORD is **Indonesian-only**.
Neither was chosen for resemblance to our production mix.

**Q2. Why 999 invoice documents and 100 receipts?**
Invoices: 20 instances × 50 templates is the smallest sample covering every layout, and cost
scales with documents ($80.98 as run). Receipts: 100 is the *entire* held-out test split, so
there is no sampling decision to defend. *Caveat:* n=100 gives wide intervals — receipt scalar
micro is 91.44% **[86.4, 95.9]**.

**Q3. Why seed 42, and is it reproducible?**
Seed 42 is arbitrary but **fixed and recorded in the plan file** with the exact selection rule
("positions 0–9 of each cluster's seed-42 shuffle"). Re-running the plan reproduces the document
set exactly. Receipts need no seed. *Caveat:* a single seed gives no estimate of
sampling-induced variance; slice 3 exists and is unrun, which would provide one.

**Q4. Is this representative of real-world documents?**
**No, and it should not be presented as such.** *Evidence:* FATURA is synthetically rendered
from 50 templates with no scan noise, skew or photography artefacts; CORD is phone photographs
of Indonesian receipts with blurred headers. *Caveat:* these are upper bounds on invoice
performance and locale-specific on receipts. Treat as regression baselines and defect finders,
not as forecasts.

**Q5. Why are keys excluded from evaluation?**
Three distinct reasons, all published per document with counts: (a) the dataset cannot speak to
the path — 30 receipt paths, 44 invoice paths; (b) the annotation is genuinely ambiguous —
3,071 invoice `currency` fields with only a bare `$`; (c) the **schema** cannot hold the fact —
1,200 invoice documents with multi-rate tax against a scalar `Totals`. *Caveat:* (c) is a
product limitation being excluded from a product benchmark; it should be reported as a schema
gap, which §A3 does.

**Q6. Why is exact match appropriate, and why for every key?**
These values are posted to a ledger: a total off by a cent is wrong, and so is an address
missing its postcode. Using one rule everywhere also makes one number mean one thing — under the
previous scoring, "99.5%" meant *exact* for numbers and *within ANLS 0.8* for addresses, so a
reader had to know which key they were looking at. *Evidence:* `scoring.match_policy: exact`;
the measured cost is **−0.50pp** on invoices and **−2.8pp** on receipt line-item cells, and it
relocated the worst invoice keys from email to **address** (`shipTo` 100.00% → 92.18%).
*Caveat:* "exact" still means exact **after normalisation** — case, whitespace and unicode form
are not what is being measured, and `addressStructured` components are merged before comparison
because which component a model chose is not a correctness question. If the team wants byte
equality instead, that is a different and stricter policy, and one flag away
(`match_policy: threshold` restores the old behaviour for comparison).

**Q7. Why different normalisation per field, if the metric is one thing?**
Because *strictness* is uniform while *normalisation* cannot be. A phone number and an address
are both compared with exact equality; what differs is that one is reduced to digits and the
other is merged and case-folded first. *Evidence:* the rule table in §A4 is driven by the
schema's **declared type**, not the field name, so it cannot drift as fields are added; dates
are scored twice and never pooled — ISO as the fact, raw as format fidelity. *Caveat:* the
receipt corpus additionally uses a **verbatim** value policy (§B4); that is a per-dataset
decision and the reason invoice and receipt headlines are not comparable.

**Q8. How are missing, extra and ambiguous values handled?**
Three-state nulls, structurally. Annotated-with-value counts toward accuracy; annotated-as-absent
makes an emission a **hallucination**; not-annotated leaves every denominator. *Evidence:*
invoices found 89–91 hallucinations; receipts report **0 by construction**, and the report says
so rather than claiming a perfect result. *Caveat:* CORD licenses no authoritative absence, so
receipts have **no hallucination measurement at all**.

**Q9. How are line items evaluated?**
Rows are **aligned**, never compared index-wise: exact assignment on identifying text (ANLS ≥
0.8) with numeric tie-breaks; then every cell of a paired row is scored by **exact** match, only
where GT states a value. The ANLS threshold survives *only* as the pairing device — requiring
exact text to pair two rows would leave most rows unmatched and their cells unscoreable, which
measures nothing. Row precision/recall/F1 and cell accuracy are always published together, plus
`table_exact` (56.0%), which cannot be gamed by dropping a hard row. *Caveat:* 24 discount cells
were excluded because two rows were indistinguishable to the aligner — published with a reason,
not silently dropped.

**Q10. Why does performance change between arms?**
Different work happens at each. *Evidence (invoice):* RAW → RAW_PP is +0.46pp and entirely
deterministic — `discountTotal` 73.64% → 100%. *Evidence (receipt):* RAW_PP → FINAL is +25.67pp
because 23–31% of totals are `null` in RAW and 1–2% in FINAL. *Caveat:* RAW_PP is a scoring
construct; production never builds that document.

**Q11. Is the judge's improvement significant?**
**Practically: on receipts yes, on invoices no.** *Evidence:* receipts +57 of 222 fields
(+25.67pp) with detector F1 89.4%; invoices +31 of 13,676 (+0.24pp) with detector precision
9.83%. *Caveat:* no formal significance test has been run. The invoice intervals
**[98.3, 99.2]** for RAW_PP and **[98.6, 99.3]** for FINAL **overlap**, so the invoice gain is
not distinguishable from noise at this n; the receipt intervals [56.5, 74.6] and [86.4, 95.9]
do not overlap.

**Q12. Can the judge introduce regressions?**
**Yes, measured.** *Evidence:* invoices **58 fields harmed** — `totals.taxName` `GST` →
`GST(18%)`, `dueDate` `18-Mar-1998` → `18-Mar-2006`, a correct total `504.89` → `(9%)`, and its
worst key `shipTo.addressStructured` at **−2.23pp**. Receipts 1 harmed — `42,000` → `.42,000`.
*Caveat:* harm rates are low (0.40% and 0.68%); the concern is severity, not frequency.

**Q13. Which keys are the main weaknesses?**
Invoices: **address fields** (`shipTo` 92.18%, `seller` 95.85%, `customer` 96.08%), then email
(97.6–98.0%), `taxName` (98.63%, judge-harmed), and `seller.name` hallucination (90 of 999).
Receipts: `unitPrice` 89.5%, `taxes[].value` 89.7%, `description` 91.1%, plus the money cells at
*amount* level (locale defect). *Caveat:* three receipt keys (n=5–6) are excluded from all
conclusions as unreportable. *Observed:* the invoice weakness moved from email to address purely
because of the metric change — the underlying predictions are identical.

**Q14. Could annotation quality affect the results?**
**Yes, and it was checked rather than assumed.** *Evidence:* the receipt corpus passed a
round-trip audit (100/100 records rebuild identically), an arithmetic audit (0 off-by-1000
errors), and spot verification against three receipt images. FATURA has 5 recorded GT defects,
excluded by name. CORD's own transcription is inconsistent within `test-13` (`3.000` and
`3,000`). *Caveat:* an arithmetic audit **cannot** detect a uniform scale error, because scaling
is linear — that is why the dot-thousands question was settled by three independent tests
instead (§B4).

**Q15. How do these relate to production performance?**
**Loosely, and only as bounds.** *Evidence:* the measured system is frozen at recorded commits
and the images are the same `quality_clean_jpeg` bytes production feeds the extractor, so the
pipeline is faithful; the *documents* are not. *Caveat:* FATURA is synthetic (expect production
below 99.03%); CORD is one locale (a locale-specific defect may be over- or under-represented).

**Q16. What are the main limitations?**
(1) **No invoice line items** — FATURA annotates none. (2) **No receipt header** — CORD annotates
none and blurs it. (3) **No hallucination measurement on receipts.** (4) Receipt n=100, wide
intervals, 3 unreportable keys. (5) Two different value policies, so the two headlines are not
comparable. (6) The receipt field map is `1.0-draft` and **unsigned**. (7) One model version, one
seed, no repeat runs. (8) FATURA is synthetic.

**Q17. What further experiments would strengthen this?**
In order of value: (a) **run SROIE** — 626 real receipts with header annotations, closing
limitation (2) and giving a hallucination denominator; (b) **an invoice corpus with line items**
(DocILE is a candidate and an archived adapter exists); (c) **re-run the invoice arm with the
dot-thousands fix** to confirm the 26pp attribution; (d) **slice 3** of the FATURA seed-42 plan,
already built and unrun, for a sampling-variance estimate at zero design cost; (e) two repeat
runs at fixed seed to bound model non-determinism; (f) sign off the receipt map to `1.0-frozen`.

**Q18. Was the benchmark itself validated?**
**Yes, and it found four of its own errors before they became conclusions.** *Evidence:* an
oracle test (GT fed back as a prediction with rows reversed) scores 100.0% of 1,034 cells and
aligns every row, while a negative control that adds 1 to every number drops to 48.26%. During
the smoke run the harness caught: an invented English alignment key scoring correct answers as
row misses; a tautological `key` cell scoring 100% by construction; a formula-injection bug that
baked `#VALUE!` into a delivered workbook; and a manifest that recorded the wrong field-map
version for every run. *Caveat:* these were found on **dev**; the test split has seen no
instrument amendment.

## Reproduction

```bash
# Invoices
python3 scripts/build_gt.py --doc-type invoice  --dataset fatura
python3 steps/step5_run.py --plan gt/invoice/fatura/main_500_seed42_slice1.json --run-id s42_slice1
python3 steps/step5_run.py --plan gt/invoice/fatura/main_500_seed42_slice2.json --run-id s42_slice2
python3 scripts/merge_runs.py runs/s42_slice1 runs/s42_slice2 --out runs/s42_main1000
python3 steps/step7_metrics.py runs/s42_main1000 --dataset fatura

# Receipts
python3 scripts/prepare_cord_v2.py --out ../../Benchmark/data/CORD_v2
python3 scripts/build_gt.py --doc-type receipt --dataset cord_verbatim
python3 scripts/plan_by_split.py --doc-type receipt --dataset cord_verbatim --split test --name main
python3 steps/step5_run.py --plan gt/receipt/cord_verbatim/cord_verbatim_test_main_100.json \
    --run-id cord_test_main_100_verbatim
python3 steps/step7_metrics.py runs/cord_test_main_100_verbatim --dataset cord_verbatim
python3 steps/step6_compare.py runs/cord_test_main_100_verbatim --dataset cord_verbatim

# Verify any document's ground truth against its source
python3 scripts/verify_gt.py --dataset cord_verbatim --doc test-32 --run runs/cord_test_main_100_verbatim
python3 scripts/verify_gt.py --dataset cord_verbatim --audit --split test
```

Response caching means re-scoring after a metric change costs nothing. Every number in this
document comes from `runs/<id>/results.json`.
