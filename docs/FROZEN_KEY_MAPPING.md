# FATURA → DocuXray key mapping — evidence-checked

**Status:** every mapped row below is confirmed by a value-level comparison of 16 real DocuXray
outputs against the ground truth for the same documents (Template1–16, Instance0).
**Zero mapping changes were required.** The blind draft held.

**Method.** For each GT label with a value, the comparison generated its component "atoms" (the
whole string, each number, each percentage, the leading word) and searched the model output for
any schema path holding a matching value. This reports where the model *actually put* each fact.
The map was written before any of this was run, so the evidence tests it rather than defines it.

`n` is the number of the 16 documents where the GT label was present.

---

## Confirmed mappings

| FATURA GT label | → DocuXray schema path | n | agreement |
|---|---|---|---|
| `DATE` | `invoiceInfo.issueDate` + `invoiceInfo.issueDateISO` | 16 | 16/16 |
| `DUE_DATE` | `invoiceInfo.dueDate` + `invoiceInfo.dueDateISO` | 11 | 11/11 |
| `NUMBER` | `invoiceInfo.documentNumber` | 13 | 13/13 |
| `PO_NUMBER` | `invoiceInfo.purchaseOrderNumber` | 4 | 4/4 |
| `TOTAL` | `totals.totalIncludingTax` (+ `currency`) | 15 | 15/15 |
| `AMOUNT_DUE` | `totals.totalIncludingTax` (dedup with TOTAL) | 0 | — not in this sample |
| `SUB_TOTAL` | `totals.subtotal` (+ `currency`) | 11 | 11/11 |
| `TAX` | `totals.taxName` + `totals.taxPercentage` + `totals.taxAmount` (+ `currency`) | 6 | 6/6 all three |
| `GST(n%)` | `totals.taxName`="GST" + `totals.taxPercentage`(from key) + `totals.taxAmount` | 2 | 2/2 |
| `DISCOUNT` | `totals.discountPercentage` + `totals.discountTotal` | 7 | 7/7 both |
| `SELLER.Name` | `parties.seller.name` | 13 | 13/13 |
| `SELLER.Address` | `parties.seller.addressStructured.address` | 11 | 11/11 |
| `SELLER.Email` | `parties.seller.email` | 9 | 7/9 |
| `BUYER.Name` | `parties.customer.name` | 12 | 12/12 |
| `BUYER.Address` | `parties.customer.addressStructured.address` | 12 | 12/12 |
| `BUYER.Tel` | `parties.customer.phone` | 12 | 12/12 |
| `BUYER.Email` | `parties.customer.email` | 12 | 12/12 |
| `BILL_TO.*` | `parties.customer.*` (only when BUYER absent) | 2 | 2/2 |
| `SEND_TO.*` | `parties.shipTo.*` | 1 | 1/1 |
| `NOTE` | `invoiceInfo.customerMemo` | 10 | 6/10 — model recall, not a mapping fault |

`currency` has no GT label of its own; it is parsed out of the money strings (`725.30 EUR`).
A bare `$` is ambiguous and leaves currency unscored on that document.

## Confirmed unmapped — the model never emits these anywhere

Searched every schema path in all 16 outputs. Not one of these values appears.

| FATURA GT label | n | schema reason |
|---|---|---|
| `SELLER.Site` `BUYER.Site` `BILL_TO.Site` `SEND_TO.Site` | 18 | `Party` has no website field |
| `PAYMENT_DETAILS.*` (bank, branch, account, SWIFT) | 16 | no bank/remittance group in `InvoiceData` |
| `GSTIN` `GSTIN_SELLER` `GSTIN_BUYER` | 5 | no tax-ID field on `Party` |
| `TOTAL_WORDS` | 7 | no field, no product use |
| `CONDITIONS` | 6 | truncated boilerplate; `paymentTerms.raw_text` forbids inferred terms (model used it 2/6) |
| `TITLE` | 12 | classifier signal — matched `documentType` 5/12, a different metric |
| `LINE_ITEMS_TEXT` | — | conversion artifact |

These are the four schema gaps, now empirically confirmed rather than inferred: **party website,
party tax ID, bank/remittance details, multi-rate tax.**

---

## Corrections this comparison forced

### 1. The `SELLER.Address == BUYER.Address` exclusion was wrong — retracted

I had excluded 27 documents where the seller and buyer address matched, calling it source
cross-population. Inspecting `images/Template1_Instance0.jpg` shows **the page genuinely prints
the same address and email in both the seller block and the Bill-to block**. Ground truth is
right, the model is right, and excluding the field would have thrown away a correct extraction.
The exclusion is removed; the duplication is now only flagged in `meta` so a report can slice it.

### 2. The pilot sampler was biased — fixed

All 27 occurrences are on **Instance0** files (27 of the 31 Instance0 docs carrying both parties);
**zero** of the other 6,169 documents show it. FATURA seeded each template's fixed seller block
from that template's Instance0 buyer. Taking the lexicographically first document per key-set —
which is what the pilot did — would have loaded the hand-adjudication set with a quirk present on
0.4% of the corpus. The selector now takes a **mid-range instance per key-set**, still
deterministic.

This generalises: *anything* derived from Instance0 files is suspect on this dataset.

### 3. Ground truth loses line items the model gets right

`Template1_Instance0` is `_line_items_status: "header_but_no_rows"`, yet the page clearly prints
five rows and DocuXray extracted all five correctly with quantities and prices. The GT recovery
failed, not the model. Confirms the standing rule: line items are scored **only** on the 3,363
`ok` documents, absence is never authoritative, and no line-item number goes in a headline.

---

## Model behaviours worth knowing before the run

* **Seller/customer bleed.** The model copies the buyer address into `parties.seller` on 9/16 and
  the email on 6/16 — but the GT shows the same duplication at nearly the same rate, and the page
  really does print it. Not an error here. On a real-world dataset it would be worth re-checking.
* **`taxPercentage.normalizedValue` is sometimes null while `originalValue` holds `(3.88%)`** —
  the postprocessor cannot parse a parenthesised percentage. Seen on Templates 1, 3, 6. This is
  exactly the postprocessor number-parsing failure the fallback rate is designed to count, and it
  is why `NUMERIC_SOURCE = "originalValue"` is the right default.
* **`NOTE` recall is 6/10.** A model gap, not a mapping gap.
* **`TOTAL` occasionally also lands in `subtotal` (2/15) or `totalExcludingTax` (1/15)** — usually
  because the page prints one figure that serves as both. Row alignment will handle it; worth
  watching in adjudication.
