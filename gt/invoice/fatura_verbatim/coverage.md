# fatura_verbatim — ground-truth coverage

10000 documents · 50 clusters · 99 key-sets · 0 annotated line-item rows

25 scoreable paths, of which 23 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `currency` | 9200 | 46 | 3 | 0 | no |
| `invoiceInfo.documentNumber` | 8800 | 44 | 8794 | 0 | yes |
| `invoiceInfo.dueDate` | 5800 | 29 | 4507 | 2 | yes |
| `invoiceInfo.dueDateISO` | 5800 | 29 | 4507 | 2 | yes |
| `invoiceInfo.issueDate` | 9798 | 49 | 6516 | 0 | yes |
| `invoiceInfo.issueDateISO` | 9798 | 49 | 6516 | 0 | yes |
| `invoiceInfo.purchaseOrderNumber` | 1400 | 7 | 100 | 0 | yes |
| `parties.customer.addressStructured` | 7400 | 37 | 7400 | 0 | yes |
| `parties.customer.email` | 7400 | 37 | 7290 | 0 | yes |
| `parties.customer.name` | 7400 | 37 | 7052 | 0 | yes |
| `parties.customer.phone` | 7400 | 37 | 7400 | 0 | yes |
| `parties.seller.addressStructured` | 10000 | 50 | 41 | 1800 | yes |
| `parties.seller.email` | 10000 | 50 | 22 | 5600 | yes |
| `parties.seller.name` | 10000 | 50 | 34 | 3200 | yes |
| `parties.shipTo.addressStructured` | 1800 | 9 | 1800 | 0 | yes |
| `parties.shipTo.email` | 1800 | 9 | 1789 | 0 | yes |
| `parties.shipTo.name` | 1800 | 9 | 1783 | 0 | yes |
| `parties.shipTo.phone` | 1800 | 9 | 1800 | 0 | yes |
| `totals.discountPercentage` | 2400 | 12 | 399 | 0 | yes |
| `totals.discountTotal` | 2400 | 12 | 1789 | 0 | yes |
| `totals.subtotal` | 6800 | 34 | 6742 | 3 | yes |
| `totals.taxAmount` | 4400 | 22 | 4053 | 0 | yes |
| `totals.taxName` | 4400 | 22 | 2 | 0 | no |
| `totals.taxPercentage` | 4400 | 22 | 404 | 0 | yes |
| `totals.totalIncludingTax` | 8399 | 42 | 8297 | 0 | yes |

## Exclusions recorded, with reasons

* **800** — BUYER and BILL_TO both present; schema has one customer slot
* **600** — multi-rate tax: InvoiceData.Totals is scalar and cannot hold 5 tax lines (SCHEMA
* **600** — multi-rate tax: InvoiceData.Totals is scalar and cannot hold 2 tax lines (SCHEMA
* **4** — GT DATE holds a due date
* **1** — GT TOTAL holds 'DUE_AMOUNT : 240.38 $'
