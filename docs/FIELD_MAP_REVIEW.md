# FATURA field map — review sheet

**For:** Naveen + one second reviewer (name needed)
**Time:** ~90 minutes each
**Artifact under review:** `mapping/fatura_field_map.yaml` (v0.1.2-draft, 29 rows)
**Rule:** the map was authored **blind** — from FATURA label semantics read against the
`Field(description=...)` text in `new_schema.py`. No DocuXray model output was inspected.
Please review it the same way. Once signed off it is frozen; later changes go in the CHANGELOG
with a reason.

---

## How to review

22 of 29 rows are marked `confidence: high` and should take a few seconds each — scan and accept.
Your time belongs on the **five questions in §2** and the **coverage accounting in §3**.

Set `review:` on each row to `accepted` or `disputed`. For disputes write one line saying why.

---

## 1. What the map does

| | Count |
|---|---|
| FATURA labels mapped to schema paths | 21 |
| Labels unmapped (reason published verbatim) | 8 |
| Schema leaf paths under `InvoiceData` (generated) | 72 |
| Paths marked `not_annotated` (excluded from every denominator) | 27 |
| Paths actually scoreable once the corpus is built | **26** |
| Of those, headline-eligible | **21** |
| Rows resolved by evidence rather than judgement | 3 |
| Rows needing your decision | 5 |

Three rows I had flagged as arguable in the earlier critique are now **resolved by measurement**,
not opinion:

- **`AMOUNT_DUE`** equals `TOTAL` in **399/399** files where both appear → it is a restatement of
  the same fact. Maps to `totals.totalIncludingTax`, deduplicated against `TOTAL`. The
  `postPaymentRemainingCurrentBalance` and `grandTotalIncludingPreviousBalances` alternatives
  both imply prior-balance semantics FATURA never expresses.
- **`DISCOUNT` sign** → GT stored as **positive magnitude**. Reason:
  `invoice_postprocessor._normalize_totals` converts a negative `discountTotal` to its absolute
  value. Storing GT as `-13.42` would make arm B score worse than arm A on every discounted
  invoice as a pure sign artifact. Discount amount == subtotal × pct in **1800/1800** files, so
  both the percentage and the amount are internally consistent and safe to score.
- **`CONDITIONS`** → unmapped. Every value is the same truncated boilerplate
  (`"Terms and Conditions\nwill be charged if payment is not made within the due date."` — the
  source has lost the late-fee clause), and `paymentTerms.raw_text`'s own description forbids
  inferring terms from a due date.

---

## 2. The five questions

### Q1 — `SUB_TOTAL` → `totals.subtotal` or `totals.totalExcludingTax`?

The schema has both. FATURA prints a literal `SUB_TOTAL` label, which argues for `totals.subtotal`.
But `total.py`'s prompt describes `subtotal` with a spatial cascade ("the verbatim printed base sum
of items, extracted exclusively from within the visual totals block"), and on some templates the
printed sub-total sits outside that block.

- **Proposed:** `totals.subtotal`, with `totals.totalExcludingTax` recorded as `alternative_target`.
- **Option:** dual-score and publish the spread. Costs one extra column, buys an honest answer.
- **Your call:** ☐ accept proposed ☐ dual-score ☐ use `totalExcludingTax`

---

### Q2 — Addresses: how do we score a multi-line block against five components?

GT gives one block: `"16424 Timothy Mission\nMarkville, AK 58294 US"`.
The schema splits it into `address` / `city` / `state` / `postal_code` / `country`.

- **Proposed:** score `addressStructured.address` against the whole GT block at ANLS ≥ 0.8, and
  mark the other four `not_annotated`.
- **Rejected:** parse the GT block into components ourselves — our parser would then define
  correctness, which is the circularity problem in a different costume.
- **Optional extra:** a containment check (every GT address token appears somewhere across the
  model's five components) measures component coverage without our parser choosing the split.
  Cheap; report as a secondary line.
- **Your call:** ☐ accept proposed ☐ proposed + containment check ☐ something else

This affects `parties.seller`, `parties.customer` and `parties.shipTo` — 3 of the 7 party paths.

---

### Q3 — Multi-rate tax: 200 files are unrepresentable

200 files carry **five simultaneous GST rates** (1%, 5%, 12%, 18%, 20%). Another 200 carry both a
`TAX` and a `GST(n%)` label. `InvoiceData.Totals` has **scalar** `taxName` / `taxAmount` /
`taxPercentage` and cannot hold more than one tax line.

(Note `ReceiptTotals.taxes` **is** a `List[ReceiptTaxes]`. Invoices are the gap, receipts are not.)

- **Proposed:** exclude those 400 files from tax scoring, record the reason as a schema gap.
- **Option:** change `Totals.taxes` to a list to match `ReceiptTotals`. Real product work, changes
  the extraction prompt, but it removes a genuine limitation rather than papering over it.
- **Your call:** ☐ exclude for v1 ☐ fix the schema first ☐ exclude now, ticket the fix

---

### Q4 — `NOTE`: map it, or drop it?

Exactly **5 distinct values** across all 5,200 files ("Thank you for your business!", "This order
is shipped through blue dart courier", …). Template-baked boilerplate; effective n = 5.

- **Proposed:** map to `invoiceInfo.customerMemo`, mark `headline_eligible: false`.
- **Option:** unmap it. It cannot support a claim either way, and mapping it adds a coverage row
  that looks more meaningful than it is.
- **Your call:** ☐ map, barred from headlines ☐ unmap

---

### Q5 — Three field groups FATURA annotates that DocuXray cannot represent

This is a **product** decision, not a benchmark one.

| Missing from `InvoiceData` | FATURA coverage | Notes |
|---|---|---|
| Party **website** | `SELLER.Site` 10,000 · `BUYER.Site` 6,200 · BILL_TO 1,600 · SEND_TO 1,800 | `Party` has name / addressStructured / phone / email. No website field anywhere. |
| Party **tax ID (GSTIN)** | `GSTIN` 1,400 · `GSTIN_SELLER` 1,000 · `GSTIN_BUYER` 600 | `Benchmarking_plan.md` §4.2 already lists "tax IDs" under the Parties group — the schema does not implement them. |
| **Bank / remittance details** | `PAYMENT_DETAILS` 2,600 (bank name, branch, account no., SWIFT) | No group exists in `InvoiceData`. |

Adding them makes those labels scoreable and means the benchmark has already paid for itself by
finding a real gap. It also means touching `extraction_prompts.py`, which is not free and would
have to land **before** the version freeze.

- **Your call, per group:**
  - Party website: ☐ add to schema ☐ leave unmapped
  - Party tax ID: ☐ add to schema ☐ leave unmapped
  - Bank details: ☐ add to schema ☐ leave unmapped

Caveat if you add tax ID: the bare `GSTIN` label (1,400 files) does not say whose it is, so it
stays unattributed and unmapped regardless. And all three GSTIN variants are template constants
(effective n = 7 / 5 / 3), so they could never carry a headline anyway.

---

## 3. Coverage accounting — what this benchmark actually measures

**Now measured, not estimated.** The ground truth has been built over all 10,000 documents; the
full generated table is `gt/coverage.md`. Sign off on this — it is the honest statement of scope
and it goes on the methodology page.

Of **72** `InvoiceData` leaf paths, **26 are scoreable on FATURA and 21 are headline-eligible**.
The other 46 are outside what the dataset annotates and are excluded from every denominator.

| Reporting group | Scoreable paths | Headline eligible |
|---|---|---|
| Document identity (`documentNumber`, `purchaseOrderNumber`) | 2 | 2 |
| Dates (`issueDate`+ISO, `dueDate`+ISO) | 4 | 4 |
| Parties — customer (name, email, phone, address) | 4 | 4 |
| Parties — shipTo (name, email, phone, address) | 4 | 4 |
| Parties — seller (name, email, address) | 3 | **0** |
| Totals (total, subtotal, tax ×3, discount ×2) | 7 | 6 |
| Currency | 1 | 1 |
| Memo (`customerMemo`) | 1 | **0** |
| Line items (description, quantity, unitPrice) | 3 | **0** (agreed caveat) |

Five paths are scoreable but **barred from headlines**, each for a stated reason:

| path | why | support |
|---|---|---|
| `parties.seller.name` | template constant — identical in all 34 templates it appears in | 6,800 docs, **34 distinct values** |
| `parties.seller.addressStructured.address` | template constant across all 41 templates | 8,173 docs, **41 distinct values** |
| `parties.seller.email` | template constant across all 22 templates | 4,384 docs, **22 distinct values** |
| `invoiceInfo.customerMemo` | 5 distinct values in the whole corpus | 5,200 docs, **5 distinct values** |
| `totals.taxName` | 2 distinct values, and constant within every template | 4,400 docs, **2 distinct values** |

The bar has two independent tests, because they catch different things: too few distinct GT
values, **or** identical across every instance of every template. The seller block passes the
first (34–41 distinct values, above the floor of 20) and fails the second — which is exactly the
trap, since a distinct-value count alone would have waved it through.

### Exclusions the build actually applied

| exclusion | documents | paths dropped |
|---|---|---|
| bare `$` as the only currency evidence | 3,071 | `currency` |
| BUYER + BILL_TO collision | 200 | 4 × `parties.customer.*` |
| multi-rate tax (5 GST rates) | 200 | 3 × `totals.tax*` |
| TAX and GST both present | 200 | 3 × `totals.tax*` |
| `SELLER.Address` == `BUYER.Address` | 27 | `parties.seller.addressStructured.address` |
| `SELLER.Email` == `BUYER.Email` | 16 | `parties.seller.email` |
| known GT label swaps | 3 | dates / total on 3 named documents |

The `$` exclusion is the big one: **3,071 of 10,000 documents cannot have their currency
scored** because FATURA states no locale and `$` could be USD, CAD, AUD or SGD. Guessing USD
would inflate the currency number on nearly a third of the corpus. Flag if you disagree.

## 4. Two things that must be settled before the pilot run

**Version freeze.** Which Gemini model ID and `prompt_version` does the run pin to, and can you
undertake that `ai/extraction/extraction_prompts.py` and `ai/judge/core/judge_prompts.py` do not
change until the pilot is scored? A prompt change mid-run makes the numbers unusable.

**Spend ceiling.** 1,099 documents (99 pilot + 1,000 main) × 4 extraction calls, plus sectional judge on arm C. Give me a
figure and I will size concurrency and abort thresholds to it.

---

## 5. Sign-off

| | Name | Date | Verdict |
|---|---|---|---|
| Author (blind draft) | Claude | 2026-09-01 | drafted; v0.1.2 after two contract-check fixes |
| Reviewer 1 | Naveen Kumar | | |
| Reviewer 2 | *(name needed)* | | |

Once both reviewers sign, bump `version` to `1.0-frozen` in the YAML and the map becomes the
contract. Phase 1 (normalisers) starts from it.
