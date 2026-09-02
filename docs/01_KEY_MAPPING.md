# Step 1 — key mapping

**Goal:** a reviewed, frozen table from the dataset's labels to DocuXray schema paths.
**Cost:** nothing. **Output:** `mapping/<dataset>_<doctype>.map.yaml`.

## Why this step decides whether the benchmark means anything

The tempting shortcut is to run the model, look at where it put each value, and write the map
to match. Do that and every ambiguous field resolves in the model's favour — you are measuring
"did the model do what the model did". The bias is invisible in the final numbers.

**So the map is authored blind:** from the dataset's label semantics read against the
`Field(description=...)` text in `new_schema.py`, before any model output is inspected.

Model output is used afterwards only to *test* the map. When the two disagree, that is a
finding to adjudicate — not a licence to move the target.

## How to do it

```bash
# what the dataset actually contains, with coverage and samples
python steps/step1_key_mapping.py --doc-type invoice --dataset-labels

# what the schema offers, with the match rule each path will get
python steps/step1_key_mapping.py --doc-type invoice --schema-paths

# once the map is written: does the code still agree with it?
python steps/step1_key_mapping.py --doc-type invoice --check
```

`mapping/FATURA_key_mapping_worksheet.xlsx` is the reviewable form: labels down the left,
a dropdown of every valid schema path, blank columns to fill in. Two reviewers sign it.

## Five things that will bite you

**1. One label is often several facts.** `TAX = "VAT (3.88%): 28.18 EUR"` is a tax name, a rate,
an amount and a currency. The map is one-to-many with a parser, not a rename. On FATURA that was
about 60% of the real work.

**2. One fact is sometimes several fields.** DocuXray routes note text by content — a payment
sentence to `paymentTerms.raw_text`, everything else to `customerMemo`, and a mixed note split
across both. No single target is right. That is what `DocTypeSpec.merged_targets` is for.

**3. The schema describes what the MODEL may emit, not what the PIPELINE produces.**
`NumericValue` declares only `originalValue`; the postprocessor adds `normalizedValue`
afterwards. Pinning the scorer to a stage-added field would have scored arm A as zero on every
amount. Diff a real raw output against a real refined output and enumerate every key that only
appears in the latter.

**4. Some labels have no home, and saying so is a result.** FATURA annotates party websites,
party tax IDs and bank details; `InvoiceData` has none of them. Those are published schema gaps,
not mapping failures.

**5. Absence is not always authoritative.** Before treating a missing label as "the field is not
on the page", check five documents per label. If the dataset's labelling is exhaustive, absence
gives you a real hallucination denominator — which is rare and valuable.

## The contract check

`check_contract()` asserts the executable rule table and the reviewed map agree on every
label's status, and that every parser named exists. It **gates ground-truth builds**: if the
code has drifted from the signed-off map, nothing is written.

It earned its place twice on FATURA — catching a label the map expressed only as prose, and
three labels the adapter resolved that the map never named. Resolve a failure by amending one
side with a changelog entry. Never by loosening the check.
