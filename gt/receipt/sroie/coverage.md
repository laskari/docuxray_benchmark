# sroie — ground-truth coverage

958 documents · 958 clusters · 2 key-sets · 0 annotated line-item rows

5 scoreable paths, of which 5 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `parties.seller.addressStructured` | 957 | 957 | 408 | 1 | yes |
| `parties.seller.name` | 957 | 957 | 316 | 0 | yes |
| `receiptInfo.txnDate` | 958 | 958 | 702 | 0 | yes |
| `receiptInfo.txnDateISO` | 957 | 957 | 428 | 0 | yes |
| `totals.totalIncludingTax` | 958 | 958 | 635 | 1 | yes |

## Exclusions recorded, with reasons

* **2** — conflicting GT labels
* **1** — GT value present but unparseable (date)
