# Receipt benchmarking plan — SROIE + CORD-v2

Status: rev 3 (2026-09-07). CORD-v2 is now ON DISK and measured — §3 and §4 below are counts from all 1,000 documents, not estimates. Rev 2 was written before the data existed and got two mappings wrong; both are corrected here.

Status: rev 2 (2026-09-07). Rev 1 planned against the flattened `Benchmark/data/CORD`
folder; this revision switches CORD to **CORD-v2** (`clovaai/cord`, released as
`naver-clova-ix/cord-v2`) and is the version to work from.

**Headline finding: `core/` does not need to change for either dataset.** The harness already
carries everything both need — a `RECEIPT` DocTypeSpec, row-aligned list scoring driven entirely
by adapter metadata, and per-dataset ground-truth directories. SROIE is already wired and has a
50-document run behind it. CORD is two new files plus five small edits.

---

## 0. Status — 2026-09-07 23:30 UTC

**P0b and P2 are done, up to the model run.** Nothing below has cost a single model call.

| done | evidence |
|---|---|
| CORD-v2 materialised | `Benchmark/data/CORD_v2/{train,dev,test}` — 800 / 100 / 100, all 1,000 `gt_parse` from CORD itself, 0 missing images |
| `datasets/cord_parsers.py` | parses **99.45%** of 7,050 money values; of 39 refusals, 32 are values CORD emitted as lists |
| `tests/test_cord_parsers.py` | 59 tests, every case a value that occurs in the corpus, with its census count |
| `mapping/cord_receipt.map.yaml` | 26 labels, all with an explicit status: 15 mapped, 11 unmapped, each with a written reason |
| `datasets/cord_receipt.py` | split-qualified doc ids, row + keyed-totals sections, contract check |
| `registry.py` / `config.yaml` | `cord` entry and `paths.dataset_cord` |
| `scripts/plan_by_split.py` | split-aware, table-size-stratified plans |
| `gt/receipt/cord/` | 1,000 records, **2,573 GT rows**, 8 scoreable paths, all 8 headline-eligible |
| plans | `cord_dev_smoke_10` · `cord_dev_pilot_20` (disjoint from smoke) · `cord_test_main_100` (251 rows) |

**Three things the data changed, none of them from looking at a score**

* `menu.discountprice` is an amount, not a percent — 99 of 101 values (rev 2 had this backwards).
* `menu.vatyn` is free text, not a taxability code — all 3 values (rev 2 mapped it on the name alone).
* 5 documents contain two line items identical on every key the aligner can see, differing only
  in a discount — 10 rows, 0.39%. The aligner is free to pair those either way, so the cell would
  have scored by coin flip. The adapter now drops that cell with a published reason. Detected off
  `core.rows.TIEBREAK_KEYS` rather than hard-coded, so a change there cannot leave it stale.

**Oracle self-test (no model calls).** A prediction built from ground truth with **the rows
reversed** and the line total routed to the `IncludingTax` leaf scores 100.00% of cells and
aligns every row, on all three plans — 1,034 cells and 367 rows on the test plan. A negative
control that adds 1 to every number drops to 48.26%. The row plumbing is therefore verified
before anything is spent.

**Regression guard.** `pytest tests/` → 254 passed, 7 skipped, 1 failed: `test_pins.py`, which
needs `docuxray_ai_backend` and fails only because that folder is not mounted in this sandbox —
unrelated to these changes. Of the files git reports as modified, only four carry a timestamp
from this session (`registry.py`, `config.yaml`, `scripts/build_gt.py`,
`docs/NEW_DOCTYPE_CHECKLIST.md`). `core/`, `doctypes/` and every FATURA artefact are untouched.

> **Warning about the guard in §7.** The working tree was ALREADY dirty against HEAD before this
> session: `core/*`, `datasets/fatura_*`, `doctypes/__init__.py`, `mapping/fatura_invoice.map.yaml`
> and `gt/invoice/fatura/*` all carry uncommitted edits dated 5–7 September, and the DocILE100
> files are deleted but not committed. So `git diff --stat ... must be EMPTY` cannot work as
> written. **Commit a baseline before the next change**, or the guard silently passes nothing.

**Two edits to shared files, both additive:**

* `scripts/build_gt.py` gained `--root`, to override the config's relative dataset path when a
  sandbox mounts the data elsewhere. Recorded in `coverage.json` as `root_overridden`.
* `core/runner.py` now takes the dataset from the PLAN when the caller did not pass `--dataset`,
  and refuses outright when an explicit `--dataset` contradicts the plan. 30 added lines, no
  behaviour change for a plan without a `dataset` key (every older FATURA plan), verified by the
  suite going 254 -> 255 passed. This closes a real hazard: running a cord plan while
  `run.dataset` still said `sroie` failed loudly here only because the two id spaces do not
  overlap — two datasets whose ids DO overlap would have scored every document against the wrong
  ground truth and reported a number instead of an error.
* `docs/NEW_DOCTYPE_CHECKLIST.md`'s receipt examples gained `--dataset cord`. They would now fail
  without it — `dataset_for` deliberately refuses an ambiguous doc type, and `receipt` has two
  datasets as of today. `RUN_NOW.sh` was audited and is invoice-only, so it needs no change.

**Next:** `steps/step5_run.py --plan gt/receipt/cord/cord_dev_smoke_10.json` in your own Terminal
(Gemini is not reachable from the sandbox). ~$1.20. Then the 20-doc dev pilot, adjudicate, amend
the map with a CHANGELOG entry if needed, and only then the 100-doc test run.


### Dev smoke run — `runs/cord_smoke`, 10 documents, $0.87

10/10 ok, 0 failed, 0 timed out, all three arms. The gate did its job: it found **two instrument
bugs and one product defect**, and the instrument bugs were making the product look worse than
it is.

**Instrument bug 1 — keyed totals were aligned on an invented English key.** `totals.subtotal`,
`.taxes` and `.otherCharges` are `{key, value}` lists aligned on the key's text, and the map set
that key to the literal `subtotal` / `tax` / `service`. The extractor emits the caption the
receipt actually prints: `SUBTTL`, `PB1`, `SVC CHG 6%`. So 3 of 7 subtotals, 2 of 5 taxes and 1
of 3 service charges aligned — and **every row that did align was 100% correct on every cell**.
That pattern (low row recall, perfect cell accuracy) is the signature of a bad alignment key
rather than a bad model. The captions were recoverable: `gt_parse` drops them, but they are the
`is_key` words in `valid_line`, which `prepare_cord_v2.py` keeps. Fixed → 5 of 6, 4 of 4, 3 of 3
of the rows the model emitted.

**Instrument bug 2 — a tax caption carries a rate the schema models separately.** `PB1 10%` vs
the extractor's `PB1` is ANLS 0.43, a miss. But the extractor also emits
`percentage: "10%"`, and `ReceiptTotals.taxes[]` has a `percentage` field. Splitting the caption
is what the schema asks for. `totals.taxes[].percentage` moved out of `not_annotated` and scores
2 of 2. `totals.otherCharges` is deliberately NOT split — no percentage field there, so
`SVC CHG 6%` is the whole name and splitting it would break an alignment that works.

**Product defect — Indonesian dot-thousands are read 1000× too small.** Of the 23 wrong
line-item cells in arm RAW, **21 are this one bug**; the other 2 are the model emitting nothing.
They concentrate in the 3 of 10 receipts that print `5.000` rather than `5,000`.

Verified against the page, not inferred: `dev/images/26.png` reads `NASI MERAH/PUTIH 1x 5.000`
… `Total Rp. 35.000`, and 5+8+2+14+6 = 35 thousand. The extractor returns
`unitPrice.originalValue "5.000"` and — decisively — the **product's own**
`normalizedValue: 5.0`. So this is not the benchmark's parser disagreeing: the product itself
resolves five thousand rupiah to five, and arms RAW, RAW_POSTPROCESSED and FINAL are identical
because the postprocessor makes the same call. Line-item cell accuracy is 80.7%; without this
one locale bug it would be 98.3% (117 of 119).

**Also fixed:** `core/runner.py` never set `cfg["_map_path"]`, so `_map_version` fell through to
its default and every run — SROIE's and CORD's alike — recorded
`mapping/fatura_invoice.map.yaml`'s version in its manifest. `runs/cord_smoke`'s manifest
therefore claims field map `1.0-frozen` when the CORD map is `1.0-draft`. One line; future runs
record the map they were actually scored against.

Both map amendments are recorded in the map's CHANGELOG, were made on **dev**, and changed the
instrument rather than a threshold. The test split has seen no amendment.


---

## 1. What is already in place (verified, not assumed)

| thing | state |
|---|---|
| `doctypes.RECEIPT` | declared, `list_paths = {lineItems, totals.subtotal, totals.taxes, totals.otherCharges}`, `date_leaves = {txnDate, serviceDate}` |
| `schema/receipt_leaf_paths.tsv` | generated, 50 leaf paths under `ReceiptData` |
| `datasets/sroie_receipt.py` + `mapping/sroie_receipt.map.yaml` | exist, 4 labels mapped |
| `gt/receipt/sroie/` | `ground_truth.jsonl` for **626 documents**, coverage written |
| `runs/sanity_check-20260907-092554` | 50 SROIE docs, RAW + FINAL, **FINAL micro recall 95.0%**, $5.63 ($0.11258/doc) |
| `core/rows.py` + `core/metrics.py::score_document_lists` | Hungarian row alignment; row P/R/F1 **and** cell accuracy on matched rows, aggregated per section |
| `registry.py` | already has a commented-out `cord` entry and a `gt_subdir` mechanism so two receipt datasets cannot collide |

The line-item scorer is driven **only** by adapter-supplied metadata:

```
gt_rec.gt["lineItems"]                     -> list of GT row dicts
gt_rec.meta["row_targets"]["lineItems"]    -> {gt_row_key: (pred_leaf, pred_leaf, ...)}   # union
gt_rec.meta["row_match_keys"]["lineItems"] -> which GT keys carry identifying text
gt_rec.meta["row_text_leaves"]["lineItems"]-> which prediction leaves carry identifying text
```

A dataset that does not put a `list_path` in `annotated_fields` is skipped silently. **That is
why FATURA passes through untouched** and why adding CORD line items cannot affect it.

---

## 2. Blockers to resolve before writing any CORD code

### B1 — Two SROIE copies with different annotation key casing

`config.yaml` points `dataset_sroie` at `../../Benchmark/sroie_data/SROIE`. The adapter's
`SCALAR_RULES` expect lowercase `company / date / address / total`. The copy at
`Benchmark/data/SROIE` — 626 annotations, 626 images — uses **`Store_name`, `Date`,
`Store_addr`, `Total`**, and additionally carries `line_items`, `line_items_source`,
`line_items_reconciled` and `ocr_lines`.

Existing coverage shows 626 documents *with values*, so the build read the lowercase copy. If
anyone repoints the config at `data/SROIE` without touching the adapter, every label falls
through `if label not in SCALAR_RULES: continue` and ground truth silently becomes 626 documents
of `ABSENT` — no error, no failing test.

**Recommendation:** make `data/SROIE` canonical (richer copy, and the one you pointed me at) and
alias explicitly rather than with `.lower()` — casing should be declared, not inferred:

```python
LABEL_ALIASES = {"Store_name": "company", "Store_addr": "address",
                 "Date": "date", "Total": "total"}
```

Then assert in `check_contract` that every label seen in the corpus is in `SCALAR_RULES`,
`LABEL_ALIASES`, or `UNMAPPED_LABELS`. This class of silence cannot then recur.

### B2 — `receiptInfo.txnDateISO` is effectively broken on SROIE

`gt/receipt/sroie/coverage.md` records **621 exclusions: "GT value present but unparseable
(date)"**, and `txnDateISO` supported by only **5 documents**. SROIE dates are day-first
(`25/12/2018`); `date_to_iso` is not parsing them. Currently invisible in headlines only because
5 < the 30-document bar.

- **(a)** give SROIE a day-first parser, score ISO as the fact and `txnDate` as format fidelity —
  the split the FATURA map already uses for `DATE`; or
- **(b)** declare `txnDateISO` `not_annotated` for SROIE, reason written into the map.

Do not leave it: 621 exclusions in a 626-document corpus reads as a corpus defect to an auditor.

### B3 — "step 2" does not build ground truth  *(diagnosis corrected 23:40)*

`steps/step4_sampling.py` is a **symlink** to `steps/step2_ground_truth.py` — one file serving
two step numbers, deliberately, not the copy-paste accident an earlier revision of this document
called it. Apologies for the false alarm.

The real issue is narrower and still worth fixing. That shared file re-emits sampling plans and
**requires `ground_truth.jsonl` to already exist**; it does not build it. The builder is
`scripts/build_gt.py`. So `python steps/step2_ground_truth.py --doc-type receipt` — the command
`docs/NEW_DOCTYPE_CHECKLIST.md` gave for step 2 — cannot produce ground truth, and SROIE's was
in fact built by a one-off, `scripts/build_sroie_gt_and_samples.py`, which **skips
`check_contract`, skips the image-existence pre-flight, and does not write coverage**.

CORD's ground truth was built through `build_gt.py` and did not repeat that. The checklist's
step-2 line has been corrected to point at `build_gt.py`. Renaming the shared file to something
like `steps/emit_plans.py` (keeping both step symlinks) would stop the name promising something
it does not do, but that is cosmetic and can wait.

### B4 — Neither dataset licenses authoritative absence

All four SROIE labels are `absence_authoritative: false`, so the hallucination figure is
structurally zero, and `results.md` already says so. Correct — keep it and keep the caveat. CORD
is the same for its header fields and must be treated identically.

### B5 — CORD-v2 has to be downloaded on a machine with HuggingFace access

`huggingface.co` is refused by your org's egress policy from this session (403 at the proxy,
both in the cloud container and in the Cowork VM), so I could not fetch it. Run this in a normal
terminal on a machine that can reach it:

```bash
pip install datasets pyarrow
python3 scripts/prepare_cord_v2.py --out ../../Benchmark/data/CORD_v2
# or, if you download the parquet by hand:
python3 scripts/prepare_cord_v2.py --parquet-dir ~/Downloads/cord-v2 --out ../../Benchmark/data/CORD_v2
```

`scripts/prepare_cord_v2.py` is written and attached. It materialises:

```
Benchmark/data/CORD_v2/<split>/images/<doc_id>.png
Benchmark/data/CORD_v2/<split>/annotations/<doc_id>.json
```

Expected: **train 800 · dev 100 · test 100**. The script prints those counts; a mismatch is a
stop.

---

## 3. What the two datasets actually contain

### SROIE — `Benchmark/data/SROIE` — 626 annotations, 626 JPEGs

| label | docs |
|---|--:|
| `Store_name` | 626 |
| `Date` | 626 |
| `Store_addr` | 625 |
| `Total` | 625 |

Also per file: `line_items[]` (`line_number`, `Prod_item`, `Prod_quantity`, `Prod_price`),
`line_items_source: "derived_from_ocr_layout"`, `line_items_reconciled: true`, `ocr_lines[]`.

**Those line items are derived from OCR layout, not official SROIE ground truth.** Scoring
against them measures agreement with a heuristic, not with the document. Leave `lineItems` out of
SROIE's `annotated_fields` for v1; if you want the number later, publish it as a labelled
secondary with its provenance stated, never in a headline. SROIE then stays exactly the 4-key
task you described — and CORD-v2 is where line items get measured properly.

### CORD-v2 — 1,000 receipts, 800 / 100 / 100

Annotation is word-level with a 30-label taxonomy across five superclasses:

| superclass | categories |
|---|---|
| `menu` (14) | `nm`, `num`, `unitprice`, `cnt`, `discountprice`, `price`, `itemsubtotal`, `vatyn`, `etc`, `sub_nm`, `sub_unitprice`, `sub_cnt`, `sub_price`, `sub_etc` |
| `void_menu` (2) | `nm`, `price` |
| `subtotal` (6) | `subtotal_price`, `discount_price`, `service_price`, `othersvc_price`, `tax_price`, `etc` |
| `total` (8) | `total_price`, `total_etc`, `cashprice`, `changeprice`, `creditcardprice`, `emoneyprice`, `menutype_cnt`, `menuqty_cnt` |
| `void_total` (0) | removed from the active dataset |

Each `valid_line` entry carries `words[] {quad, is_key, row_id, text}`, `category`, `group_id`
and — **new in v2** — `sub_group_id`. Plus `meta {version, image_id, split, image_size}`, `roi`,
`repeating_symbol`, `dontcare`.

**There is no store-name, address, phone, date or document-number category anywhere in the
taxonomy.** Your premise is confirmed from two directions: the label set has no header fields at
all, and visually — `CORD/images/12.png` — the header block and footer are blurred while the
line-item block and totals are legible. So the header is not merely unscoreable, it is
*unannotated*, which the harness already handles as state (iii): out of every denominator, no
core change required.

#### Why v2 and not the flattened `Benchmark/data/CORD` folder

That folder is CORD's 800-document train split with the annotation flattened to `menu_0_nm`-style
keys. Measured against all 800 files, the flattening costs four things, each of which the
benchmark needs:

| lost | consequence |
|---|---|
| `group_id` | row grouping is re-encoded as an index in the key name, and an **unindexed** key is used when a receipt has one row (`menu_nm` in 336 files vs `menu_0_nm` in 456) — two parsing branches instead of a field |
| `sub_group_id` | v2's sub-item hierarchy is unrecoverable; the flat form has both `menu_sub_N_nm` and `menu_N_sub_N_nm` with no consistent meaning |
| `is_key` | printed captions are not separated from values. This is visible as corruption: `total_menuqty_cnt` is stored as **`"(2"`** and `menu_N_unitprice` as **`"@120,000"`** |
| `row_id` / `quad` | no reading order or geometry, so a disputed row alignment cannot be adjudicated |

v2 additionally **corrected mislabels present in v1**, and carries dev and test — without which
there is no held-out measurement at all.

`prepare_cord_v2.py` writes both representations per document: `gt_parse` (CORD's own nested
`{menu:[…], sub_total:{…}, total:{…}}`, what the adapter reads) and `valid_line` (the audit
trail). Where a record has no `gt_parse` the script rebuilds an equivalent from `valid_line` by
`(group_id, sub_group_id, category)`, dropping `is_key` words, and stamps
`gt_parse_source: "rebuilt"` so provenance is never silently mixed.

**Value formatting remains the biggest correctness risk.** Indonesian receipts use either
separator for thousands: `"120,000"` and `"60.000"` both mean 60–120 thousand, not 120. Also
`"0%"` / `"100%"` in `discountprice`, `"-60.000"` for a negative discount. A naive `float()`
turns 120,000 into 120. (Dropping `is_key` removes the `@` and `(` artifacts, which is one class
of this problem the flat copy could not fix.)

---

## 4. Key mapping

### SROIE → `ReceiptData` (4 keys, unchanged apart from B1/B2)

| label | schema path | parser | match rule |
|---|---|---|---|
| `Store_name` / `company` | `parties.seller.name` | `passthrough_strip` | `text_contained` |
| `Store_addr` / `address` | `parties.seller.addressStructured` | `address_whole` | `address`, ANLS 0.8 merged; consumes the 4 component leaves |
| `Date` / `date` | `receiptInfo.txnDate` (+ `txnDateISO` if B2(a)) | `passthrough_strip` / day-first ISO | `date_raw` / `date_iso` |
| `Total` / `total` | `totals.totalIncludingTax` | `money_amount` | `numeric` |

Everything else stays in the map's `not_annotated` block, as it already is.

### CORD-v2 → `ReceiptData` (line items + totals only)

**Line items** — `gt["lineItems"]`, one row per `menu` `group_id`, in `group_id` order:

| GT row key | CORD-v2 category | prediction leaves (union) |
|---|---|---|
| `description` | `menu.nm` | `lineItems[].description` |
| `itemCode` | `menu.num` | `lineItems[].itemCode` |
| `quantity` | `menu.cnt` | `lineItems[].quantity` |
| `unitPrice` | `menu.unitprice` | `lineItems[].unitPrice` |
| `lineTotal` | `menu.price`, else `menu.itemsubtotal` | `lineItems[].lineTotalIncludingTax`, `lineItems[].lineTotalExcludingTax` |
| `discountAmount` | `menu.discountprice` when it is **not** a `%` (99 of 101 values) | `lineItems[].discountAmount` |
| `discountPercent` | `menu.discountprice` when it ends in `%` (2 of 101) | `lineItems[].discountPercent` |

`lineTotal` is deliberately a **union of two prediction leaves**: CORD does not say whether the
printed line amount is tax-inclusive, so the model must not be penalised for its routing choice.
This is exactly what `row_targets`' union semantics exist for — the best-matching leaf wins and
the winner is recorded in the cell record.

**Corrected from rev 2**, on the measured corpus:

* `menu.discountprice` is an **amount**, not a percent — 99 of 101 values, printed with a
  trailing minus (`'800-'`, `'1,400 -'`). Rev 2 said "map to `discountPercent` only when it ends
  in `%`", which would have discarded 99 real discounts and, worse, scored the 2 percents
  against an amount field. Both targets are now mapped, chosen per value by the `%`.
* `menu.vatyn` is **not** a Y/N flag. All 3 corpus values are free text — `'Sales included PB1'`,
  `'10% Tax Included'` — so `lineItems[].lineTaxabilityCode` is the wrong home. Declared
  `not_annotated`, reason recorded.

Row identity: `row_match_keys = ("description", "itemCode")`,
`row_text_leaves = ("description", "itemCode")`, threshold 0.8 (the module default).

Excluded from rows in v1, each with a written reason in the map:

- `menu.sub_*` sub-items — `ReceiptData` has no sub-item concept; folding them into rows would
  invent ground truth. Keep them in `gt_parse` under `sub` so the decision is reversible.
- `void_menu.*` — voided lines.
- `menu.etc` (9 values) and `menu.vatyn` (3, free text) — no schema home.
- **Any leaf CORD emitted as a LIST** — 32 values corpus-wide, where the receipt printed the same
  category twice. Two subtotals is a genuinely ambiguous fact; excluded per document with a
  reason rather than picked. `menu.nm` is the one exception: a list there is a name read across
  two lines, and is joined, because dropping it would remove a scoreable row.

**Totals** — `ReceiptTotals.subtotal`, `.taxes` and `.otherCharges` are **lists of `{key, value}`**
on receipts where the invoice equivalents are scalars. Annotate them as one-row sections matched
on `key`, not as scalars:

| CORD-v2 category | target | shape |
|---|---|---|
| `total.total_price` | `totals.totalIncludingTax` | scalar numeric — CORD's strongest field (775/800 in train) |
| `subtotal.subtotal_price` | `totals.subtotal[]` | row `{key: "subtotal", value: N}` |
| `subtotal.tax_price` | `totals.taxes[]` | row `{key: "tax", value: N}` |
| `subtotal.service_price` | `totals.otherCharges[]` | row `{key: "service", value: N}` |
| `subtotal.othersvc_price` | `totals.otherCharges[]` | row `{key: "other service", value: N}` |
| `subtotal.discount_price` | `totals.discountTotal` | scalar numeric, sign normalised with `abs` (CORD prints it negative), documented |
| `total.cashprice` | `totals.cash` | scalar numeric |
| `total.changeprice` | `totals.change` | scalar numeric |

`not_annotated` for CORD, with reasons: every `parties.*` and `receiptInfo.*` path (blurred in
source images); `total.creditcardprice`, `total.emoneyprice`, `total.menuqty_cnt`,
`total.menutype_cnt`, `subtotal.etc`, `total.total_etc` (no schema home);
`totals.totalExcludingTax`, `totals.roundingAdjustment`, `applyTaxAfterDiscount`, `currency`.

**Cluster id:** CORD has no templates. Use the document itself, as SROIE does, and say so in the
coverage report — intervals then resample documents, the honest treatment for a natural corpus
and the reason the report already prints "clusters" beside "docs".

**Number parsing:** write `cord_money` in a **new** `datasets/cord_parsers.py` (do not edit
`fatura_parsers.py`), with an explicit documented rule for `,` vs `.` as thousands separator, and
a unit test over `120,000`, `60.000`, `5.455`, `-60.000`, `0`. Validate it on the pilot with the
arithmetic identity `sum(line totals) ≈ subtotal_price` per document — that check finds a
mis-parsed separator immediately and costs nothing, because it needs no model calls.

---

## 5. Sampling

Both corpora are one-document-per-cluster, so `--instances-per-cluster` degenerates. Emit
explicit seeded plans through `build_gt.py` so `purpose` / `selection` / `seed` land in the plan
file instead of a one-off script.

- **SROIE:** 100 of 626, seed pinned, as a **superset of the existing 50-document
  `sanity_check` plan** — those 50 are already in `_cache`, so the run costs about half.
- **CORD-v2:** this is the payoff for using v2. Build ground truth for all three splits, then:
  - **pilot** — 20 from **dev**, one per distinct key-set, to shake out the single-row,
    sub-item and separator branches and to validate the map;
  - **main** — **100 from `test`**, the reported numbers, on documents no map decision was
    tuned against;
  - **train** — built but not run. It is the reserve if you later want a 500-document figure.

Reporting the headline on the held-out test split is a materially stronger claim than sampling
from train, and it costs nothing extra now that the splits exist.

**Cost:** measured at **$0.11258/doc**. 100 SROIE ≈ $5.6 incremental, 100 CORD ≈ $11.3, pilot 20
≈ $2.3. Well inside the $250 cap.

---

## 6. Exact change list

**New files — zero blast radius on FATURA**

```
scripts/prepare_cord_v2.py       written, attached — CORD-v2 -> on-disk layout
datasets/cord_receipt.py         CordAdapter, iter_labels, check_contract  (reads gt_parse)
datasets/cord_parsers.py         cord_money, pct_only, vatyn
mapping/cord_receipt.map.yaml    authored BLIND, before looking at model output
tests/test_cord_adapter.py       copy of tests/test_fatura_adapter.py
tests/test_cord_parsers.py       the separator cases above
```

**Edited files — small and additive**

| file | change | risk |
|---|---|---|
| `registry.py` | implement the `cord` `DatasetEntry`, `gt_subdir="cord"` | **one behavioural change:** `dataset_for` deliberately raises once `receipt` has two datasets and none is named. Audit `RUN_NOW.sh` and every script for a receipt invocation that omits `--dataset`. |
| `config.yaml` | add `paths.dataset_cord: "../../Benchmark/data/CORD_v2"`; fix `paths.dataset_sroie` (B1); keep `run.dataset` explicit | low |
| `datasets/sroie_receipt.py` | `LABEL_ALIASES`, stricter `check_contract`, B2 date decision | rebuilds SROIE GT — expected |
| `mapping/sroie_receipt.map.yaml` | mirror the alias / ISO decision | `check_contract` fails if these two drift — that is the guard working |
| `steps/step2_ground_truth.py` | restore as a wrapper over `scripts/build_gt.py` (B3) | low |

The CORD adapter needs one wrinkle the others do not: **split-aware roots**. Either give
`CordAdapter` a `split` argument and register `cord` once with `iter_raw` walking
`<root>/<split>/annotations`, or register `cord_test` / `cord_dev` as separate entries. Prefer the
first — `image_path` then stays relative to the dataset root as `datasets/base.py` requires
(`<split>/images/<id>.png`), and one registry entry covers all three splits.

**Do not touch:** `core/*`, `doctypes/__init__.py` (RECEIPT is already correct),
`mapping/fatura_invoice.map.yaml`, `gt/invoice/fatura/*`, `_cache/` key composition.

If you find yourself needing to edit `core/` for CORD, the missing thing is a `DocTypeSpec`
field — the rule the codebase already states, and it has held so far.

---

## 7. FATURA regression guard

Run before the first change and after each phase. The last two steps re-score from cache and cost
nothing.

```bash
pytest tests/ -q                                   # esp. test_fatura_adapter, test_corpus,
                                                   # test_registry_paths, test_pipeline_parity,
                                                   # test_three_arms, test_rows, test_metrics
python3 scripts/build_gt.py --doc-type invoice --dataset fatura
git diff --stat gt/invoice/fatura/ground_truth.jsonl     # must be EMPTY

python3 steps/step6_compare.py runs/main_a
python3 steps/step7_metrics.py runs/main_a
git diff --stat runs/main_a/results.md                   # must be EMPTY

git status --short gt/invoice mapping/fatura_invoice.map.yaml   # must be EMPTY
```

A non-empty diff on any of those four is stop-and-investigate, not rebase-and-carry-on.

---

## 8. Phases

| phase | work | exit criterion |
|---|---|---|
| **P0** | Resolve B1–B4. Rebuild SROIE GT through `build_gt.py`. | regression guard clean; SROIE coverage shows 0 unexplained exclusions |
| **P0b** | Download CORD-v2 and run `prepare_cord_v2.py` (B5) | prints train 800 · dev 100 · test 100; `gt_parse_source` is `cord` for all of them |
| **P1** | SROIE 100-doc plan + run + report | 4 fields, all headline-eligible, RAW / RAW_PP / FINAL, CI from a document bootstrap |
| **P2** | CORD adapter + blind map + `cord_money` tests + pilot 20 on **dev** | pilot arithmetic identity holds; map amended only with changelog entries |
| **P3** | CORD 100-doc run on **test** + report | row P/R/F1 **and** cell accuracy on matched rows published together, per the module's own rule |
| **P4** | Combined receipt report across both datasets | header fields visibly absent from CORD denominators; per-dataset numbers never pooled |

Keep P2's map authored blind. The checklist is explicit that copying the invoice map and renaming
fields is the failure mode, and CORD's `menu.*` vocabulary is far enough from invoice semantics
that a copied map would look plausible and be wrong.

---

## 9. Decisions I need from you

1. **Which SROIE copy is canonical** — `Benchmark/data/SROIE` (626 files, capitalised keys, has
   line items) or `Benchmark/sroie_data/SROIE` (what the config points at today)? I could not read
   the second; it is outside the folders shared with this session.
2. **SROIE `txnDateISO`** — fix the parser day-first (B2a), or declare it not-annotated (B2b)?
3. **SROIE OCR-derived `line_items`** — leave out of v1 as recommended, or score as a caveated
   secondary?
4. **CORD sub-items and `discountprice` without `%`** — exclude in v1 as recommended, or map now?
5. **CORD split for the headline** — I have planned pilot on dev, reported numbers on test, train
   held in reserve. Confirm, or say if you want the 800-document train figure instead.
6. **The old `Benchmark/data/CORD` folder** — retire it once CORD_v2 lands, or keep both? Keeping
   both invites someone to point the config at the wrong one, which is exactly how B1 happened.
