# Superseded — plans for the 626-file SROIE corpus

These plans address documents by the ids `000`..`625`, which were assigned by the
zzzDavid/ICDAR-2019-SROIE GitHub repo the old corpus came from. They are **not runnable against
the current ground truth**: `gt/receipt/sroie/ground_truth.jsonl` now covers 958 documents
filed under their original ICDAR ids (`X51005268262`), so `core/runner.py` stops with
"plan documents are not in the ground truth" rather than measuring anything.

Kept because the numbers already reported were measured from them — chiefly `tranche_626.json`,
the whole-corpus tranche the SROIE figures in the map changelog come from.

## Translating an old id

`../../../../../Benchmark/sroie_data/SROIE_FULL/manifest.json` carries `old_id_to_doc_id`,
built by matching image bytes, e.g. `124` -> `X51005677328`. All 626 old files map;
the 626 files fold to 621 distinct images because the old corpus itself shipped 5 images twice.

`../sroie_legacy626_main_621.json` is `tranche_626.json` already translated, so the old
measurement can be recomputed on the new corpus and compared like for like. The response cache
is keyed on image bytes, so recomputing it costs nothing.

## Files

* `batch_1.json`
* `batch_2.json`
* `main_626.json`
* `pilot_2.json`
* `sanity_check.json`
* `smoke_626.json`
* `tranche_10.json`
* `tranche_100.json`
* `tranche_500.json`
* `tranche_626.json`
