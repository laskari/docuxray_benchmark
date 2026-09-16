# sroie — ground-truth coverage

626 documents · 626 clusters · 2 key-sets · 0 annotated line-item rows

5 scoreable paths, of which 4 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `parties.seller.addressStructured` | 626 | 626 | 291 | 1 | yes |
| `parties.seller.name` | 626 | 626 | 236 | 0 | yes |
| `receiptInfo.txnDate` | 626 | 626 | 493 | 0 | yes |
| `receiptInfo.txnDateISO` | 5 | 5 | 4 | 0 | no |
| `totals.totalIncludingTax` | 626 | 626 | 448 | 1 | yes |

## Exclusions recorded, with reasons

* **621** — GT value present but unparseable (date)
