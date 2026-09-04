# docile100 — ground-truth coverage

100 documents · 100 clusters · 3 key-sets · 450 annotated line-item rows

10 scoreable paths, of which 7 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `invoiceInfo.documentNumber` | 89 | 89 | 89 | 0 | yes |
| `invoiceInfo.issueDate` | 95 | 95 | 94 | 0 | yes |
| `invoiceInfo.issueDateISO` | 71 | 71 | 67 | 0 | yes |
| `lineItems` | 97 | 97 | 97 | 0 | yes |
| `parties.seller.name` | 100 | 100 | 92 | 0 | yes |
| `totals.discountTotal` | 9 | 9 | 8 | 0 | no |
| `totals.otherCharges` | 6 | 6 | 5 | 0 | no |
| `totals.subtotal` | 52 | 52 | 49 | 0 | yes |
| `totals.taxAmount` | 10 | 10 | 5 | 0 | no |
| `totals.totalIncludingTax` | 89 | 89 | 85 | 0 | yes |

## Exclusions recorded, with reasons

* **44** — day and month are both <= 12 and differ, so M/D vs D/M cannot be resolved; DocIL
* **3** — document has no itemised table (0 GT rows)
* **1** — GT value embeds a time component, which invoiceInfo.issueDate's own schema descr
