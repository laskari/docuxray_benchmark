# SROIE benchmark results — the full task-3 corpus

`runs/sroie_full958` · **958 documents, the whole ICDAR-2019 SROIE task-3 set** ·
**4,785 scored field instances** · three arms · three criteria ·
models gemini-3-flash-preview / gemini-3-flash-preview · image prep `quality_clean_jpeg` ·
field map **1.1-draft** (contracts 1.12–1.18 pending two-reviewer sign-off) ·
git ai_backend `3be4376`, backend `2e04452`, frontend `def05e9` · cost $48.59

**Everything here is measured on all 958 documents.** There is no train/test split in this
report. The corpus carries one — reconstructed from image bytes, 621/337, no image on both
sides — and it was used to check that the map generalises before the map was frozen. That check
passed and is recorded in §7.5; every published figure below is the whole corpus.

**This supersedes every SROIE figure published before 2026-09-21.** Two things changed beyond
the corpus: the `contains` criterion moved from the seller name to the address, and `lenient`
stopped being a similarity score. Both are in §2, with what they cost.

---

## 1. The corpus

The full ICDAR-2019 SROIE task-3 set: **973 files → 958 distinct documents**, under SROIE's own
identifiers. Three things about the published mirror had to be decided rather than assumed, and
each is recorded in `scripts/prepare_sroie_full.py` and map contract 1.15:

* **15 duplicate pairs** — byte-identical images published under two identifiers. One record
  per distinct image; the folded ids are in `SROIE_FULL/duplicates.json`.
* **The mirror's own 70/15/15 re-split leaks.** Five images sit on both sides of it, so it
  cannot be reported from. The **original ICDAR boundary was reconstructed by image bytes**:
  621 train / 337 test, no image on both sides. It is used for the generalisation check in
  §7.5 and nowhere else.
* **5 duplicate pairs disagree with themselves on ground truth.** Three differences are
  notational and collapse under the benchmark's comparison normalisers (`RM7.42` / `7.42`);
  they are resolved and logged. Two are substantive, so the corpus declines to pick an
  annotator: both readings go through the same parsers and the adapter's existing rule excludes
  the field. That is 2 of the 3 field-level exclusions in §8.

SROIE task 3 annotates four labels — `company`, `date`, `address`, `total` — which the field map
lands on five schema paths. Those five, and nothing else, are scored.

---

## 2. The three criteria

All three are reported on the **same denominator** — 4,785 field instances — so they differ only
in the numerator, and the gap between any two is exactly the one rule that widened it.

| criterion | contract | what it asks | what it forgives |
|---|---|---|---|
| **exact** | 1.6/1.7 | did the output reproduce the value as the document states it? | nothing beyond unicode form, whitespace and case |
| **contains** | 1.16 / **1.18** / **1.19** | is the ground truth present in the output **intact**? | extra text **around** the value — `seller.name` and address |
| **lenient** | **1.17** | did the output identify the value, ignoring the registration number? | containment, plus one parenthesised group containing a digit |

**No criterion forgives a number, a date, an identifier or an enum.** `totalIncludingTax`,
`txnDate` and `txnDateISO` score identically under all three, so no second column here can mean
a wrong total was excused.

### 2.1 `contains` goes to the field that over-extracts (contracts 1.18, 1.19)

Which field that is was **measured, not assumed**. Every non-exact instance of every scored
path, arm FINAL, classified by whether the ground truth appears in the prediction as a
contiguous token run:

| field | non-exact | **GT inside the prediction** | genuinely different |
|---|--:|--:|--:|
| **`parties.seller.name`** | 476 | **450  (94.5%)** | **26** |
| `parties.seller.addressStructured` | 160 | 15  (9.4%) | 145 |
| `receiptInfo.txnDate` | 12 | 4 | 8 |
| `receiptInfo.txnDateISO` | 5 | 0 | 5 |
| `totals.totalIncludingTax` | 10 | 1 | 9 |

**One field over-extracts, by two orders of magnitude in count.** `parties.seller.name` is where
the pipeline emits the registered name *plus* something else — the printed registration number,
the outlet, the brand above the legal entity — and **450 of its 476 misses are that, not a
misreading**. The address gets containment too, for a different and much smaller reason: a
receipt prints the full postal block where SROIE keeps a prefix of it (15 instances, 1.57 pp).

The last three rows are the diagnostic run for completeness, and they are **not scored under
containment**: `txnDate` is a date rule and `totalIncludingTax` a numeric one, and neither
computes a containment verdict, so both report `contains` identical to `exact` (§5). The 4 and
the 1 are what containment *would* have forgiven had those rules been eligible — a receipt
printing `10 OCT 2017 TUE` against an annotated `10 OCT 2017` — and they are exactly why
eligibility is declared per rule rather than inferred from a diagnostic.

Containment matches on **token boundaries, not raw characters** — a ground truth of `MART` is a
raw substring of `SUPERMARKET`, which is a coincidence of spelling — and on the address it is
tried against every admissible rendering of the five components, so the model is not scored on
which component it filed a token in.

**What it forgives, and it is a lot.** Containment is asymmetric:
`PETRON BKT LANJAN SB ALSERKAM ENTERPRISE` contains an annotated `PETRON BKT LANJAN SB` and
passes, and so would a prediction that glued two companies together. It answers *is the right
answer in there, unbroken*, never *is this the answer*. That is why `exact` is published beside
it and never replaced by it, and why §5.1 partitions the field rather than quoting one number.

**It cannot move the exact column.** Containment on this leaf used to be delivered by routing it
to `MatchRule.TEXT_CONTAINED`, which computes `exact` with punctuation *dropped* where `TEXT`
keeps it — so switching the column on also moved the leaf's exact figure by 0.94 pp across 9
documents. The leaf now uses `TEXT_SUPERSET`, which is `TEXT` in every respect with one extra
verdict computed. `tests/test_containment.py` asserts the two rules agree on `exact` for every
pair, including the nine that caused the drift.

The containment *comparison* does drop punctuation, and that asymmetry is deliberate: a receipt
printing `Farmasi Maluri S/B(587969-U)` with no space before the bracket tokenises as one token
and would fail for a reason that has nothing to do with the question. 32 of 957 instances turn
on this alone. A criterion being looser than exact is what a criterion is for; a criterion
*changing* exact is the bug that was fixed.

### 2.2 `lenient` removes the store number, and consults no threshold (contract 1.17)

1.14's lenient was ANLS ≥ 0.8. It is gone. Lenient is now **exact equality after both sides shed
every parenthesised group containing a digit** — the business-registration number printed beside
the trading name:

```
STRIPS    SWC ENTERPRISE SDN BHD (1125830-V)      (JM0352019-K)   (SA0264195-T)
          MR. D.I.Y. SDN BHD (CO.REG :704427-T )  (Company No 559208-M)
          GARDENIA BAKERIES (KL) SDN BHD (139386 X)   (8199K)   (LLP0007299-LGN)

KEEPS     AEON CO. (M) BHD     GARDENIA BAKERIES (KL) SDN BHD
          THE STORE (MALAYSIA) SDN BHD     BOOK TA .K (TAMAN DAYA) SDN BHD
```

The rule is shaped by what the **digit** does, not by the registration formats seen so far — the
corpus prints at least six. A narrower rule keyed on `<digits>-<char>` catches 177 of the 261
mismatches this one resolves and misses every labelled and space-separated variant.

**Measured over all 958 documents: against `exact`, the rule resolves 261 `seller.name`
mismatches on arm FINAL — 267 on RAW and on RAW_POSTPROCESSED — and breaks ZERO exact matches in
any arm.** That second number is what makes it a rung of a ladder rather than a separate
tolerance, and `tests/test_lenient.py` holds both as standing checks. (Those counts are measured
against `exact`; once containment covers the same field they are absorbed by it — see §2.2's
closing note and the empty column in §5.1.) ANLS is still computed and carried on every row so the report can show how close a miss
was; since 1.17 it decides nothing, and `--anls-threshold` is inert.

The three criteria remain a **ladder**: exact ≤ contains ≤ lenient, enforced in `compare()`.

**On this corpus `lenient` and `contains` land on the identical number** — 95.53% / 95.86% — and
that is a result rather than a redundancy. Every store-number case is *also* a containment case,
because the registration number is **appended**, which leaves the ground truth intact inside the
prediction. The two columns would separate on a corpus that prints the number *inside* the name
(`GARDENIA (139386 X) BAKERIES`), which breaks containment and which the store-number rule still
handles. SROIE has no such document.

### 2.3 What these two changes cost, stated because they move a published number

Moving `seller.name` off the containment rule also moved its **exact** verdict, by **−0.94 pp**
(51.20% → 50.26%), on **9 documents**. The old rule normalised that one leaf with punctuation
dropped; the `text` rule it now carries keeps punctuation, which is what contract 1.6/1.7
specifies and what every other text field already did. The nine are spacing and punctuation
differences — `MR. D.I.Y.` against an annotated `MR. D. I. Y.`, `SDN BHD.` against `SDN. BHD.`,
`(SP}` against `(SP)`, `**Royaltea**` against `ROYALTEA`.

This was not asked for and is not an improvement in the model; it is one leaf's normalisation
being brought in line with the other four. It is stated here rather than absorbed because it
means **the headline exact figure is 0.19 pp lower than the last report's for a scoring reason,
not a pipeline reason.**

Containment on that leaf was then restored (contract 1.19) **without** taking the exact
normalisation back with it, which is the whole point of the `TEXT_SUPERSET` rule: `seller.name`
reports 50.26% exact and 97.28% contains, the same containment figure the old rule produced. The
0.19 pp stands; nothing else moved with it.

---

## 3. Headline — arm FINAL, the shipped output, 958 documents

| | | |
|---|--:|---|
| **accuracy, exact** | **86.14%** | [85.31, 87.00] |
| accuracy, `contains` | **95.86%** | [95.31, 96.39] |
| accuracy, `lenient` | 95.86% | [95.31, 96.39] |
| scored field instances | 4,785 | |
| documents | 958 | |

Intervals are a bootstrap resampling **clusters**, which on SROIE is documents — every receipt
is its own layout, unlike FATURA where 200 instances of a template are one observation.

---

## 4. The three arms

| arm | payload | model calls in it |
|---|---|---|
| **RAW** | model output + `run_extraction_postprocessing` | extraction |
| **RAW_POSTPROCESSED** | RAW + `ai.pipeline_core.postprocess_page` | extraction |
| **FINAL** | RAW + judge + refinement + **the same** `postprocess_page` | extraction, judge, refinement |

RAW_POSTPROCESSED and FINAL run the **same postprocessor call**, the one production runs last;
they differ only in whether the judge and refinement ran first. That is what makes the
RAW_POSTPROCESSED → FINAL gap the judge's.

**RAW manipulates no values.** `run_extraction_postprocessing` is the product's own pre-judge
step and is exactly what the judge is fed (`judge_worker.py:88`). Diffed leaf by leaf against
bare model output on all 958 documents: **44,331 differences, every one structural — 0 value
manipulations.** Two shapes only, in matched pairs: a null key removed, and the leaf re-expanded
as `{"originalValue": null}`. No non-null value is changed, dropped, invented or nulled.

### 4.1 All three arms, all three criteria

| arm | exact | `contains` | `lenient` |
|---|--:|--:|--:|
| RAW | 85.52%  [84.67, 86.39] | 95.53%  [94.96, 96.09] | 95.53%  [94.96, 96.09] |
| RAW_POSTPROCESSED | 85.52%  [84.67, 86.39] | 95.53%  [94.96, 96.09] | 95.53%  [94.96, 96.09] |
| **FINAL** | **86.14%**  [85.31, 87.00] | **95.86%**  [95.31, 96.39] | 95.86%  [95.31, 96.39] |

### 4.2 RAW and RAW_POSTPROCESSED are identical on every criterion — and that is a result

0 fields fixed, 0 harmed, across 4,787 comparisons. **That is a statement about the verdicts,
not about the documents.** Classified leaf by leaf, the postprocessor's effect on RAW is:

| what the postprocessor did to RAW | leaves, 958 docs | scored? |
|---|--:|---|
| added a `normalizedValue` beside an **unchanged** `originalValue` | 32,497 | key addition only |
| turned an empty list into `null` | 1,190 | no — same absence either way |
| added a key holding `null` | 5 | no — `lineItems[]` |
| rewrote an `originalValue` | 5 | no — `lineItems[]` |
| nulled a leaf | 10 | no — `lineItems[]`, `totals.taxes[]` |
| **changed any other value** | **0** | — |

So the postprocessor **changes no value the benchmark scores**; what it does is add a second
key. RAW cannot carry that key — `NumericValue` sets `extra="forbid"` — so the scorer reads
`normalizedValue` where it exists and parses `originalValue` where it does not. The two arms are
therefore read through **different number parsers**: the benchmark's on RAW, the product's on
RAW_POSTPROCESSED. This is structural, not a defect — RAW has no parsed number to read.

Exactly one scored path carries a number, `totals.totalIncludingTax`, and **the two parsers
agree on all 958 documents** — 0 rows flagged `numeric_parse_disagreement`. That agreement is
why the arms tie, and it is a result rather than a tautology: on FATURA the two parsers disagree
often enough that 65 of 125 apparent "judge fixes" were really the product's parser.

The 10 nulled leaves are a standing guard, not a finding — if that count grows into a scored
path, the judge's measured lift moves for an unrelated reason.

---

## 5. Per field — arm FINAL, all three criteria

| path | rule | n | exact | `contains` | `lenient` |
|---|---|--:|--:|--:|--:|
| `parties.seller.addressStructured` | address | 956 | 83.26%  [80.84, 85.55] | **84.83%** | 84.83% |
| `parties.seller.name` | text_superset | 957 | 50.26%  [47.02, 53.40] | **97.28%** | 97.28% |
| `receiptInfo.txnDate` | date_raw | 958 | 98.75%  [98.02, 99.37] | = | = |
| `receiptInfo.txnDateISO` | date_iso | 957 | 99.48%  [98.96, 99.90] | = | = |
| `totals.totalIncludingTax` | numeric | 957 | 98.96%  [98.33, 99.58] | = | = |

`n` is the **accuracy denominator** — instances the dataset annotates *with a value*. It is one
or two below each path's document count because a correct null and a hallucination are reported
on their own lines and sit in no accuracy denominator; the same denominator is used in §5.1, so
the two tables reconcile row by row. `=` means the criterion is identical to exact, which is the
case for every number, date and identifier by construction. **Only two fields separate at all** — the name by 47.02 pp, the
address by 1.57 pp — and both separate under `contains`, with `lenient` adding nothing on top.

### 5.1 Where the errors actually are — every scored instance partitioned

| field | n | exact | + GT intact, over-scoped | + store number only | **genuinely wrong** |
|---|--:|--:|--:|--:|--:|
| `parties.seller.addressStructured` | 956 | 796 | **15** | 0 | **145** |
| `parties.seller.name` | 957 | 481 | **450** | 0 | **26** |
| `receiptInfo.txnDate` | 958 | 946 | 0 | 0 | **12** |
| `receiptInfo.txnDateISO` | 957 | 952 | 0 | 0 | **5** |
| `totals.totalIncludingTax` | 957 | 947 | 0 | 0 | **10** |
| **total** | **4,785** | **4,122** | **465** | **0** | **198** |

**Measured.** **198 of 4,785 field instances — 4.1% — are wrong** in the sense that the value in
the output is not the value on the page. 465 of the 663 non-exact instances are the ground truth
emitted intact with extra text around it, and **450 of those 465 are `seller.name`**.

The `+ store number only` column is empty for the reason §2.2 gives: every appended registration
number leaves the ground truth intact, so containment already caught it.

**Interpreted.** `seller.name` at 50.26% exact is the benchmark's most misreadable number, and
it must be quoted with its question attached. Under exact it asks *did the output emit the name
and nothing else* — the right question if the field is a ledger key or a match against a vendor
master, the wrong one if a human reads it. **Of its 476 failures, 450 are scope and 26 are a
genuinely different name.**

> The honest one-line summary: **the model reads the seller name correctly on 97.28% of
> receipts and over-scopes it on 47%.**

The address is the mirror image. Containment buys it only **1.57 pp** (15 instances), so almost
all of its 145 failures are a genuinely wrong block rather than an over-scoped one — which is
why the two fields need different columns and neither number can stand for both.

---

## 6. Post-processing, on its own line

| | |
|---|--:|
| RAW → RAW_POSTPROCESSED, fields compared | 4,787 |
| fields fixed | **0** |
| fields harmed | **0** |
| cost | $0.00 — no model call |

Read this beside §7: whatever appears here is **not** the judge's. On this corpus it is nothing,
because both number parsers read these totals the same way (§4.2). The right reading is not
"post-processing does nothing" — it writes to every document — but "post-processing changes no
scored answer here".

---

## 7. The judge — impact across the full corpus

The question this section answers: **with every scored field of every document combined, how
much better is the output because the judge ran?** The baseline is **RAW_POSTPROCESSED**
throughout, never RAW — RAW carries no `normalizedValue`, so measuring from it credits the judge
with the product's number parser. On SROIE the two tie, so here the choice costs nothing; it is
stated because on FATURA it was worth 65 fields.

### 7.1 All fields combined — 958 documents, 4,785 scored field instances

| criterion | RAW_POSTPROCESSED | FINAL | change | fields wrong |
|---|--:|--:|--:|--:|
| **exact** | 85.52% | **86.14%** | **+0.63 pp**  [+0.27, +1.00] | 693 → **663** |
| `contains` | 95.53% | 95.86% | +0.33 pp  [+0.00, +0.67] | 214 → 198 |
| `lenient` | 95.53% | 95.86% | +0.33 pp  [+0.00, +0.67] | 214 → 198 |

| under `exact` | |
|---|--:|
| fields **fixed** | **53** |
| fields **harmed** | 23 |
| **net** | **+30** |
| harm rate | **0.56%** |
| relative error reduction | **4.3%** |
| documents whose verdicts changed | 71 of 958 (7.4%) |
| — net better / net worse / offsetting | **49** / 20 / 2 |

Intervals are a **paired** bootstrap over documents: both arms are recomputed on the same draw,
because they score the same 958 documents and agree on 98.4% of fields. Resampling them
separately and subtracting two intervals would treat that agreement as noise and overstate the
uncertainty several-fold. **100% of 2,000 resamples put the exact delta above zero**; 97.4% under
`contains`, where the interval touches zero.

**The improvement score, stated plainly: +0.63 pp of absolute accuracy, which is 4.3% of the
errors that survived extraction — 53 fields repaired against 23 broken, net +30.**

Under `contains` the judge is worth **+0.33 pp — 36 fixed against 20 harmed, net +16, and 7.5%
of the remaining errors.** The two readings answer different questions and the gap between them
is informative: **roughly half the judge's exact-match gain is it tightening the SCOPE of a
value the extractor had already read correctly**, which `contains` does not count because it did
not count the over-scoped version as wrong in the first place. The other half is real reading
errors repaired — and that half is the one that survives the stricter column.

Under `contains` the judge touches 53 documents rather than 71, net-better 34 against
net-worse 19, at a harm rate of 0.44%.

### 7.2 Where the gain comes from — per field, under `exact`

| path | n | RAW_PP | FINAL | delta | fixed | harmed |
|---|--:|--:|--:|--:|--:|--:|
| `parties.seller.name` | 957 | 48.38% | 50.26% | **+1.88** | 23 | 5 |
| `totals.totalIncludingTax` | 957 | 97.60% | **98.96%** | **+1.36** | 16 | 3 |
| `receiptInfo.txnDate` | 958 | 98.54% | 98.75% | +0.21 | 2 | 0 |
| `receiptInfo.txnDateISO` | 957 | 99.37% | 99.48% | +0.10 | 1 | 0 |
| `parties.seller.addressStructured` | 956 | 83.68% | 83.26% | **−0.42** | 11 | **15** |

Four of five fields improve. **`addressStructured` is the only field the judge makes worse**,
and it accounts for **15 of the 23 total harms** — nearly two-thirds of all damage, on one
field. Gating judge corrections on that path would remove those 15 at a cost of 11 fixes: net
**+34** instead of +30.

### 7.3 The detector — did it find the errors it was shown?

Scored against **RAW**, which is exactly what the judge was fed:

| | flagged | silent |
|---|--:|--:|
| **wrong in RAW** | 112 | 582 |
| **correct in RAW** | 184 | 3,909 |

The matrix totals **4,787**, not the 4,785 used for every accuracy figure above: it asks what the
judge was shown, so it spans every field instance both arms carry, including the correct null and
the hallucination that sit in no accuracy denominator. On the 4,785 basis the cells are
112 / 184 / 581 / 3,908 and recall reads 16.2% rather than 16.1% — a rounding difference, stated
so the two denominators in this report are never silently mixed.

| | |
|---|--:|
| detector precision | **37.8%** |
| detector recall | **16.1%** |
| detector F1 | 22.6% |

**Measured.** The judge finds **16.1% of the errors in front of it**, and **62.2% of what it
flags is not an error**. Those two numbers are the ceiling on this stage: 582 errors were shown
to it and passed.

### 7.4 Reading 7.1 against 7.3

A 16.1% detector that nonetheless ships +30 net fields is not a contradiction: the judge is
allowed to be silent, and silence costs nothing. What the harm rate bounds is the other side —
**0.56% of the fields that were already right came back wrong**, and 15 of those 23 are one
field. The trade is good; it is not good because the detector is good.

### 7.5 The generalisation check

The map's contracts 1.11–1.18 were developed against the 621 documents of the original ICDAR
train split, and had never seen the 337 of the test split. Scored separately **once**, before
the map was frozen, arm FINAL under exact: **+0.65 pp** judge lift on the held-out 337
(86.23% → 86.88%, 14 fixed against 3 harmed) against **+0.63 pp** on the full corpus — agreeing
to within **0.02 pp**.

That is the whole use of the split in this report: **the judge's lift does not depend on the
half the map was developed on.** The two halves are not reported separately anywhere else,
because after that check there is no question a split answers that the full corpus does not
answer better.

---

## 8. What these numbers do not cover

* **Anything but four labels.** SROIE task 3 annotates `company`, `date`, `address`, `total`
  and nothing else. Line items, tax, currency, buyer and document number are unannotated and
  leave every denominator rather than scoring as correct nulls. CORD-v2 measures line items.
* **Hallucinations — structurally near-zero, not a result.** SROIE licenses no authoritative
  absence: a label's not being annotated is not evidence the fact is absent from the page. The
  corpus reports 1, on a `correct_null` denominator of 2. Do not quote a hallucination rate
  from this dataset.
* **3 field instances excluded** across the corpus, each with a recorded reason: 2 for
  conflicting ground-truth labels (§1), 1 for a date the ground truth states but no parser can
  read. They are removed from the denominator, not scored as failures.
* **Whether `seller.name` over-extraction matters.** §5.1 measures it; only the consuming
  system can say whether it is a defect.
* **Whether over-scoping is a defect.** §5.1 measures it — 450 of `seller.name`'s 476 failures
  — but only the consuming system can say whether `99 SPEED MART S/B (519537-X)` is an error or
  a richer answer. If the field is a ledger key, read `exact`; if a human reads it, read
  `contains`. The report publishes both and takes no position.

## 9. Caveats

1. **The field map is `1.1-draft` and contracts 1.12–1.18 are unsigned.** They are drafted and
   measured, not accepted. No figure here is final until they carry two signatures.
2. `parties.seller.name` and `parties.seller.addressStructured` are the only fields where the
   three criteria diverge. Every other number in this report is the same under all three.
   `lenient` and `contains` are the same number on this corpus (§2.2); the lenient column is
   retained because it separates on a corpus that prints the registration number *inside* the
   name, and SROIE has no such document.
3. **The headline moved 0.19 pp for a scoring reason** — see §2.3. An older report's 86.33% and
   this one's 86.14% are the same pipeline measured two ways, not a regression.
4. Extraction ran through the **Gemini Batch API** at 50% of interactive price. The cache key is
   identical to the sync path — same model, prompt, schema and prepared bytes — so this is the
   same measurement, not a cheaper one. 0 documents fell through to a synchronous retry.
5. **Cost.** $48.59 is corpus accounting, not a single spend: the response cache is keyed on
   image bytes, so nothing already paid for was paid for twice.

## 10. Reproducing this

```bash
python steps/step5_run.py --plan gt/receipt/sroie/sroie_test_main_337.json \
    --run-id sroie_test337 --mode batch
python steps/step5_run.py --plan gt/receipt/sroie/sroie_legacy626_main_621.json \
    --run-id sroie_train621 --mode batch
python scripts/merge_runs.py runs/sroie_test337 runs/sroie_train621 --out runs/sroie_full958
python steps/step7_metrics.py runs/sroie_full958 --dataset sroie      # §3, §4.1, §5, §6, §7.3
python steps/step6_compare.py runs/sroie_full958 --dataset sroie

python scripts/judge_impact.py runs/sroie_full958                       # §7.1, §7.2
python scripts/judge_impact.py runs/sroie_full958 --criterion contains  # §7.1, second reading
python scripts/judge_impact.py runs/sroie_full958 --criterion lenient   # §7.1, third reading
python scripts/probe_arm_payload_delta.py runs/sroie_full958            # §4, §4.2
pytest tests/test_lenient.py tests/test_containment.py                  # §2.1, §2.2
```

The two runs are extracted separately only because that is how the corpus was paid for; they are
merged before anything is scored, and every figure in this report comes from the merged 958.

The reviewer workbooks (`runs/*/review_*.xlsx`) carry a `contains?` and a `lenient?` column per
arm, both blank where they agree with the headline. Filtering `FINAL lenient?` to non-blank with
`FINAL contains?` blank gives the `seller.name` rows the store-number rule rescued.
