# Step 4 — which documents to run

**Goal:** a sample that answers the question without paying for the whole corpus.
**Cost:** nothing. **Output:** `gt/<doctype>/{smoke,pilot,main}_*.json`.

```bash
python steps/step4_sampling.py --doc-type invoice --describe
python steps/step4_sampling.py --doc-type invoice --instances-per-cluster 20 --seed 20260901
```

## Three plans, three different jobs

| plan | selection | purpose |
|---|---|---|
| **smoke** | one per cluster + declared extras | verification gate — does everything work |
| **pilot** | one per distinct **key-set** | hand-adjudicate every mismatch |
| **main** | N per cluster, seeded | the reported numbers |

**Smoke is not a measurement.** It asks: does the runner reach every stage, does scoring produce
sane numbers, does the cache work, does the manifest write. On FATURA, 51 documents exercised
**26 of 26 scoreable paths and 12 of 12 policy branches** for under $5.

**Pilot samples key-sets, not clusters.** 33 of FATURA's 50 templates carry more than one label
combination — 99 distinct key-sets in total. One-per-template would leave half the schema
variants untested.

## The trap: never sample the first of anything

FATURA seeded each template's fixed seller block from that template's **Instance0** buyer. The
result: 27 of 31 Instance0 documents print an identical seller and buyer address, and **zero** of
the other 6,169 do. Taking the lexicographically first document per key-set — the obvious
deterministic rule — over-represented a 0.4%-of-corpus quirk by about a hundredfold.

The sampler now takes a **mid-range** instance. Position in a corpus is not guaranteed to be
meaningless, and on a generated dataset it usually is not.

Where a branch genuinely cannot be reached without an excluded document, the dataset declares
`extra_smoke_docs` in `registry.py` **with a stated reason** — a deliberate exception, not a
convenience.

## Sizing

Effective sample size is the number of **clusters**, not documents. FATURA has 10,000 documents
and 50 templates; going from 1,000 to 10,000 barely moves a cluster-bootstrapped interval and
costs ten times as much.

Report `n_docs` and `n_clusters` side by side, always.

## Overlap

The pilot and main plans overlap by a handful of documents. Left in and disclosed
(`pilot_overlap` in the plan file) rather than removed: dropping named documents introduces a
systematic exclusion, which is a worse bias than a declared 1% overlap.
