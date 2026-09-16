# cord — ground-truth coverage

1000 documents · 1000 clusters · 287 key-sets · 2569 annotated line-item rows

8 scoreable paths, of which 8 clear the headline bar (>= 30 documents and >= 20 distinct ground-truth values, and not constant across clusters).

| path | docs | clusters | distinct | absent | headline |
|---|---|---|---|---|---|
| `lineItems` | 994 | 994 | 838 | 0 | yes |
| `totals.cash` | 640 | 640 | 128 | 0 | yes |
| `totals.change` | 619 | 619 | 157 | 0 | yes |
| `totals.discountTotal` | 70 | 70 | 30 | 0 | yes |
| `totals.otherCharges` | 124 | 124 | 110 | 0 | yes |
| `totals.subtotal` | 662 | 662 | 486 | 0 | yes |
| `totals.taxes` | 438 | 438 | 357 | 0 | yes |
| `totals.totalIncludingTax` | 969 | 969 | 422 | 0 | yes |

## Exclusions recorded, with reasons

* **25** — GT value present but ambiguous (CORD annotated the same category twice)
* **10** — rows indistinguishable to the aligner (identical description, itemCode and tie-b
* **8** — row has no description or itemCode to align on
* **8** — the same amount is printed again under a different caption (already recorded as 
* **6** — GT value present but unparseable ('-')
* **6** — document has no alignable menu rows (0 GT rows)
* **1** — GT value present but unparseable ('Rp 57,0000')
* **1** — GT value present but unparseable ('30.273 200')
* **1** — GT value present but unparseable ('10% 4,818')
* **1** — GT value present but unparseable ('Pb1 10% 7,000')
* **1** — GT value present but unparseable ('10.000%')
* **1** — GT value present but unparseable ('Discount (0%)')
* **1** — GT value present but unparseable ('385,0000')
* **1** — GT value present but unparseable ('Discount(0%)')
