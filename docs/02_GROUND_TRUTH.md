# Step 2 — ground truth

**Goal:** every document in one canonical, portable, scoreable form.
**Cost:** nothing. **Output:** `gt/<doctype>/ground_truth.jsonl` + `coverage.{json,md}`.

```bash
python steps/step2_ground_truth.py --doc-type invoice
```

## BenchmarkRecord

```python
doc_id, source_dataset, doc_type, image_path,
cluster_id,        # the unit that REPEATS — intervals resample this, not documents
keyset_id,         # hash of the annotated label set; the pilot samples one per keyset
gt,                # schema path -> canonical value, or ABSENT
annotated_fields,  # every path this dataset can speak to for this document
excluded_fields,   # path -> why it was dropped, published verbatim
meta               # slices: no_tax_label, multi_tax, line-item research data, ...
```

## The three-state null rule

This is the highest-risk rule in the harness, so it is structural rather than conventional:

| state | meaning | counts toward |
|---|---|---|
| path in `gt`, value present | annotated and on the page | **recall** |
| path in `gt`, value `ABSENT` | annotated as not on the page | **correct-null rate**, and a value here is a **hallucination** |
| path not in `annotated_fields` | the dataset cannot tell us | **nothing** — excluded from every denominator |

Most schema paths are null on most documents. Counting "the model correctly emitted null" as a
win would inflate every number past recognition. `validate()` raises if `gt` carries a path
outside `annotated_fields`.

## Four hard-won rules

**Image paths must be relative.** Ground truth is built on one machine and run on another. An
absolute path silently breaks every run elsewhere — it did, until a test forbade it.

**Canonicalise both sides identically.** Numerics become 2dp strings, dates ISO-8601, addresses
flattened. Both the ground truth and the prediction go through the same functions.

**Exclusions carry a reason and get published.** `excluded_fields` is `path -> why`. A reader
must be able to reconstruct the denominators.

**Never silently skip an unrecognised field.** Silent skips are how coverage quietly shrinks
between runs.

## Ground truth is not automatically true

FATURA's line-item annotations recover ~29% of printed rows, and its `_line_items_status == "ok"`
flag is not a reliable filter — a document marked `ok` had 1 row against 4 on the page, verified
against the image. Those fields are captured as research data and **never scored**.

Audit before trusting. `mapping/<dataset>.gt_errors.json` records known defects with a
`correct` or `exclude` action, and one previously-recorded "defect" was **retracted** after
looking at the page — the duplication was real, printed, and the model was right.
