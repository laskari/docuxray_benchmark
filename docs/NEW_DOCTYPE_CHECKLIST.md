# Adding a document type — checklist

Worked example: **receipts**. Nothing in `core/` changes.

## 1. Declare the type — `doctypes/__init__.py`

`RECEIPT` is already there. Check its fields against `ReceiptData`:

```python
RECEIPT = DocTypeSpec(
    name="receipt", schema_model="ReceiptData", wrapper_key="receiptOutputData",
    merged_targets={},                                    # derive from a sample, see step 4 below
    date_leaves=frozenset({"txnDate", "serviceDate"}),    # receipts use txnDate, not issueDate
    list_paths=frozenset({"lineItems", "totals.subtotal",
                          "totals.taxes", "totals.otherCharges"}),
)
```

**The biggest structural difference:** `ReceiptTotals.subtotal`, `.taxes` and `.otherCharges` are
**lists** where the invoice equivalents are scalars. That is the thing most likely to break a
naive port. List paths need row alignment before per-cell scoring; a naive index-wise comparison
is meaningless.

## 2. Generate the path inventory

```bash
PYTHONPATH=../docuxray_ai_backend python core/schema_paths.py ReceiptData \
  > schema/receipt_leaf_paths.tsv
```

Generated, never hand-edited. A schema change shows up as a diff here.

## 3. Write the dataset adapter — `datasets/cord_receipt.py`

Copy `datasets/_template.py`. Implement `iter_raw`, `build`, `check_contract`, and a module-level
`iter_labels`. Read `datasets/base.py` — the three obligations in `build`'s docstring are the
ones the invoice adapter learned the hard way (relative image paths, complete
`annotated_fields`, a `cluster_id` naming what actually repeats).

For receipts, `cluster_id` is not a template. CORD has no templates — consider the source batch,
or the document itself if observations are genuinely independent. **Getting this wrong is how
confidence intervals come out too narrow.**

## 4. Author the field map — BLIND

```bash
python steps/step1_key_mapping.py --doc-type receipt --dataset-labels
python steps/step1_key_mapping.py --doc-type receipt --schema-paths
```

Write `mapping/cord_receipt.map.yaml` from label semantics against the schema's field
descriptions, **before looking at any model output**. Two reviewers sign it. Then run a 10–20
document sample and check where the pipeline actually put each fact — to *test* the map, and to
discover any `merged_targets` (one fact provably landing across several fields).

## 5. Register it — `registry.py`

```python
"cord": DatasetEntry(
    name="cord", doc_type="receipt", module="cord_receipt", cls="CordAdapter",
    root_config_key="dataset_cord", map_path="mapping/cord_receipt.map.yaml"),
```

Add `paths.dataset_cord` to `config.yaml` as a **relative** path.

## 6. Run the seven steps

```bash
python steps/step1_key_mapping.py --doc-type receipt --check
python steps/step2_ground_truth.py --doc-type receipt
python steps/step3_environment.py
python steps/step4_sampling.py --doc-type receipt --describe
python steps/step5_run.py --plan gt/receipt/smoke_*.json --arms A,B,C --limit 10 --run-id r-probe
python scripts/recost.py runs/r-probe
python steps/step6_compare.py runs/r-probe
python steps/step7_metrics.py runs/r-probe
```

## 7. Tests

Copy `tests/test_fatura_adapter.py` and `tests/test_corpus.py`. The corpus invariants —
relative image paths, no `gt` outside `annotated_fields`, canonical numeric form, structural
exclusion counts matching the register — are the ones that caught real bugs.

## Receipt-specific things to decide early

| question | why it matters |
|---|---|
| Are list totals aligned by key or by position? | `ReceiptTotals.subtotal` is a list of `{key, value}`; matching by position is meaningless |
| What is a cluster? | CORD has no templates; the wrong choice makes intervals too narrow |
| Is absence authoritative? | Decides whether you get a hallucination denominator at all |
| Which fields are barred from headlines? | Run the eligibility test before, not after, seeing the scores |
| Does the receipt postprocessor differ from the invoice one? | Arm B is `get_postprocessor("receipt")` — verify it, do not assume |

## What NOT to do

* Do not edit `core/` for a document type. If you need to, the missing thing is a `DocTypeSpec`
  field.
* Do not copy the invoice map and rename fields. Author it blind, from the receipt schema.
* Do not reuse invoice's headline-eligibility conclusions. Re-run the test.
* Do not modify the product to make it score better. The system under measurement is frozen.
