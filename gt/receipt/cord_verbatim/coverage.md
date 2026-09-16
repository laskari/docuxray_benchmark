# cord_verbatim — ground-truth coverage

1000 documents · 1000 clusters · 287 key-sets · 2569 annotated line-item rows

8 scoreable paths, of which 8 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `lineItems` | 994 | 994 | 864 | 0 | yes |
| `totals.cash` | 640 | 640 | 196 | 0 | yes |
| `totals.change` | 619 | 619 | 238 | 0 | yes |
| `totals.discountTotal` | 73 | 73 | 37 | 0 | yes |
| `totals.otherCharges` | 124 | 124 | 110 | 0 | yes |
| `totals.subtotal` | 662 | 662 | 511 | 0 | yes |
| `totals.taxes` | 445 | 445 | 365 | 0 | yes |
| `totals.totalIncludingTax` | 971 | 971 | 519 | 0 | yes |

## Exclusions recorded, with reasons

* **25** — GT value present but ambiguous (CORD annotated the same category twice)
* **24** — rows indistinguishable to the aligner (identical description, itemCode and tie-b
* **8** — row has no description or itemCode to align on
* **8** — the same amount is printed again under a different caption (already recorded as 
* **6** — document has no alignable menu rows (0 GT rows)
