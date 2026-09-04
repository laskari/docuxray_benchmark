# FATURA coverage — what this benchmark actually measures

Built from 10,000 documents. `effective_n` is the count of **distinct ground-truth
values**, not the row count: a field printed identically on all 200 instances of a
template carries one observation, not 200. Any field below the floor of
20 distinct values is barred from headline claims.

A field is barred from headlines if it has too few distinct values, OR if it is
identical across every instance of every template it appears in — the second bar is
what catches the seller block, which has 50 distinct values (passing the floor) but
is baked into the artwork, so all 200 instances are one observation.

| path | rule | present | absent | excl | effective n | templates | varying | headline |
|---|---|---:|---:|---:|---:|---:|---:|:--:|
| `currency` | currency | 6129 | 0 | 3071 | 2 | 46 | 46 | yes |
| `invoiceInfo.documentNumber` | identifier | 8800 | 0 | 0 | 8794 | 44 | 44 | yes |
| `invoiceInfo.dueDate` | date_raw | 5798 | 2 | 0 | 4507 | 29 | 29 | yes |
| `invoiceInfo.dueDateISO` | date_iso | 5798 | 2 | 0 | 4507 | 29 | 29 | yes |
| `invoiceInfo.issueDate` | date_raw | 9798 | 0 | 2 | 6516 | 49 | 49 | yes |
| `invoiceInfo.issueDateISO` | date_iso | 9798 | 0 | 2 | 6516 | 49 | 49 | yes |
| `invoiceInfo.noteText` | text_contained | 5200 | 0 | 0 | 5 | 26 | 26 | **no** |
| `invoiceInfo.purchaseOrderNumber` | identifier | 1400 | 0 | 0 | 100 | 7 | 7 | yes |
| `parties.customer.addressStructured` | address | 7400 | 0 | 200 | 7400 | 37 | 37 | yes |
| `parties.customer.email` | email | 7400 | 0 | 200 | 7290 | 37 | 37 | yes |
| `parties.customer.name` | text | 7400 | 0 | 200 | 7052 | 37 | 37 | yes |
| `parties.customer.phone` | phone | 7400 | 0 | 200 | 7400 | 37 | 37 | yes |
| `parties.seller.addressStructured` | address | 8200 | 1800 | 0 | 41 | 41 | 0 | **no** |
| `parties.seller.email` | email | 4400 | 5600 | 0 | 22 | 22 | 0 | **no** |
| `parties.seller.name` | text | 6800 | 3200 | 0 | 34 | 34 | 0 | **no** |
| `parties.shipTo.addressStructured` | address | 1800 | 0 | 0 | 1800 | 9 | 9 | yes |
| `parties.shipTo.email` | email | 1800 | 0 | 0 | 1789 | 9 | 9 | yes |
| `parties.shipTo.name` | text | 1800 | 0 | 0 | 1783 | 9 | 9 | yes |
| `parties.shipTo.phone` | phone | 1800 | 0 | 0 | 1800 | 9 | 9 | yes |
| `totals.discountPercentage` | numeric | 2400 | 0 | 0 | 399 | 12 | 12 | yes |
| `totals.discountTotal` | numeric | 2400 | 0 | 0 | 1789 | 12 | 12 | yes |
| `totals.subtotal` | numeric | 6797 | 3 | 0 | 6607 | 34 | 34 | yes |
| `totals.taxAmount` | numeric | 4400 | 0 | 400 | 3293 | 22 | 22 | yes |
| `totals.taxName` | text | 4400 | 0 | 400 | 2 | 22 | 0 | **no** |
| `totals.taxPercentage` | numeric | 4400 | 0 | 400 | 403 | 22 | 18 | yes |
| `totals.totalIncludingTax` | numeric | 8399 | 0 | 1 | 8066 | 42 | 42 | yes |

**26 of 59 `InvoiceData` leaf paths are scoreable on FATURA; 21 are headline-eligible.** The remainder are outside what the dataset annotates and are excluded from every denominator.

## Barred from headlines

| path | reason |
|---|---|
| `invoiceInfo.noteText` | only 5 distinct GT values (floor 20) |
| `parties.seller.addressStructured` | template constant: identical across every instance in all 41 templates, so n_effective is the template count, not the document count |
| `parties.seller.email` | template constant: identical across every instance in all 22 templates, so n_effective is the template count, not the document count |
| `parties.seller.name` | template constant: identical across every instance in all 34 templates, so n_effective is the template count, not the document count |
| `totals.taxName` | only 2 distinct GT values (floor 20) |

## Exclusions by reason

| path | reason | docs |
|---|---|---:|
| `currency` | only a bare '$' as currency evidence; ambiguous, not guessed | 3071 |
| `invoiceInfo.issueDate` | GT DATE holds a due date | 2 |
| `invoiceInfo.issueDateISO` | GT DATE holds a due date | 2 |
| `parties.customer.addressStructured` | BUYER and BILL_TO both present; schema has one customer slot | 200 |
| `parties.customer.email` | BUYER and BILL_TO both present; schema has one customer slot | 200 |
| `parties.customer.name` | BUYER and BILL_TO both present; schema has one customer slot | 200 |
| `parties.customer.phone` | BUYER and BILL_TO both present; schema has one customer slot | 200 |
| `totals.taxAmount` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 5 tax lines | 200 |
| `totals.taxAmount` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 2 tax lines | 200 |
| `totals.taxName` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 5 tax lines | 200 |
| `totals.taxName` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 2 tax lines | 200 |
| `totals.taxPercentage` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 5 tax lines | 200 |
| `totals.taxPercentage` | multi-rate tax: InvoiceData.Totals is scalar and cannot hold 2 tax lines | 200 |
| `totals.totalIncludingTax` | GT TOTAL holds 'DUE_AMOUNT : 240.38 $' | 1 |
