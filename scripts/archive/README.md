# scripts/archive

One-off investigations. They are kept because a report cites their output, not because
anything runs them. Nothing in `core/`, `steps/` or `tests/` imports from here, and they
are not maintained against later scoring changes. Before you quote a number, re-run the
script that produced it.

Run them from the repo root, as with every other script: `python scripts/archive/<name>.py`.

| script | what it answered | fed |
|---|---|---|
| `score_strict.py` | accuracy with the scorer's normalisation switched off | STRICT_ACCURACY_REPORT |
| `strict_controls.py` | exact on the strict ladder's 22-key denominator | STRICT_ACCURACY_REPORT |
| `strict_diag.py` | which normalisation rule separates exact from strict, per field | STRICT_ACCURACY_REPORT |
| `render_strict_report.py` | renders the strict report from the three above | STRICT_ACCURACY_REPORT |
| `verbatim_probe.py` | what a verbatim FATURA ground truth would recover | strict report §10 |
| `score_multitax.py` | the two multi-rate-tax FATURA templates, against `totals.otherCharges` | multitax slice |
| `build_multitax_xlsx.py` | review workbook for that slice | multitax slice |
| `probe_seller_name_scope.py` | seller.name score with header text stripped | SROIE map contract 1.17 |
| `build_sroie_gt_and_samples.py` | first SROIE GT build (626-file root). Superseded by `scripts/build_gt.py` | docs/08 |
| `analyze_errors.py` | early SROIE error dump, hard-coded to a run that no longer exists | — |
