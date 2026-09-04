"""The harness must run the product's stages, not its own copies of them.

The 2026-09-03 investigation compared the app's Raw JSON for two FATURA documents against
this harness's `05_postprocessed.json` and found the harness diverging from the product in
two ways that no test could have caught, because the harness re-implemented what the workers
do instead of calling it:

  1. `_postprocess` stopped after `processor.process()`. postprocessing_worker then ran
     `validate_format` + `apply_format_nulls` on top. Since that step only nulls leaves, arm
     FINAL could score a value the product ships as null -- the benchmark flattering the
     system under measurement.
  2. `_extract` fed the source file. The app feeds the quality stage's clean image, which on
     the modal path is a JPEG re-encode of the source. Different pixels, same model, and the
     resulting field differences looked like model nondeterminism.

These tests pin both to the shared definition in `ai.pipeline_core`. They need ai_backend
importable (see activate.env) and skip cleanly without it, matching the rest of the suite's
no-network, no-API-key contract -- nothing here calls a model.
"""
from __future__ import annotations

import copy

import pytest

from core import runner as R

pc = pytest.importorskip("ai.pipeline_core",
                         reason="needs ai_backend on PYTHONPATH (source activate.env)")


def _cfg(**run_overrides):
    run = {"doc_type": "invoice", "spend_cap_usd": 250.0, "concurrency": 2,
           "early_abort_after": 0,
           "stage_timeouts": {"extract": 5, "judge": 5, "free": 5}}
    run.update(run_overrides)
    return {"run": run, "cache": {"enabled": False, "dir": "_cache"},
            "models": {"extraction": "test-extract", "judge": "test-judge"}}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    return R.Runner(_cfg(), ["RAW", "FINAL"], "t")


def _invoice(**overrides):
    data = {
        "currency": "USD",
        "invoiceInfo": {"documentNumber": "INV-1", "issueDate": "30-Nov-2021",
                        "issueDateISO": "2021-11-30"},
        "lineItems": [],
        "totals": {"totalIncludingTax": {"originalValue": "100.00 $"}},
        "shippingInfo": {},
        "parties": {},
    }
    data.update(overrides)
    return data


# ------------------------------------------------------------------ the tail
def test_postprocess_is_the_shared_function_not_a_copy(runner):
    """Byte-for-byte equality with ai.pipeline_core, on data the two paths disagree about."""
    data = _invoice(invoiceStatus="probably")
    assert runner._postprocess(copy.deepcopy(data)) == \
        pc.postprocess_page(copy.deepcopy(data), "invoice").data


def test_the_missing_format_contract_step_is_back(runner):
    """The concrete regression. The old `_postprocess` was process() alone; on this document
    that keeps a value the product nulls."""
    from ai.postprocessing import get_postprocessor

    data = _invoice(invoiceStatus="probably")
    old_behaviour = get_postprocessor("invoice").process(
        copy.deepcopy(data), job_id=None, first_pass=True)
    assert old_behaviour["invoiceStatus"] == "probably", (
        "fixture no longer separates the two paths")
    assert runner._postprocess(copy.deepcopy(data))["invoiceStatus"] is None


def test_postprocess_exposes_the_worker_s_warnings_and_verdict(runner):
    """The stage dump carries them, so a reviewer can see why a leaf was nulled without
    re-running the tail."""
    runner._postprocess(_invoice(invoiceStatus="probably"))
    assert runner._last_postprocess.format_validation["invalid_paths"] == ["invoiceStatus"]


def test_stage_dump_records_the_format_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R.Runner, "_extraction_postprocessing", lambda self, raw: raw)
    monkeypatch.setattr(R.Runner, "_extract",
                        lambda self, r, img, doc: (_invoice(invoiceStatus="probably"),
                                                   {"input": 1}))
    monkeypatch.setattr(R.Runner, "_verify",
                        lambda self, r, img, doc, unwrapped, jkey: {"judge_passed": True,
                                                                    "total_issues": 0})
    monkeypatch.setattr(R, "_run_with_timeout", lambda fn, t, label: fn())
    import ai.refinement.pipeline as refine
    monkeypatch.setattr(refine, "run_refinement_pipeline",
                        lambda data, report, dt: {"refined_data": data})

    image = tmp_path / "Template1_Instance1.png"
    image.write_bytes(b"stub bytes -- _extract is stubbed, nothing decodes this")
    runner = R.Runner(_cfg(), ["FINAL"], "t")
    r = runner.run_document("Template1_Instance1", str(image))

    assert r.ok, r.error
    import json
    body = json.loads(
        (runner.stages_dir / "Template1_Instance1" / "05_postprocessed.json").read_text())
    assert body["arm"] == "FINAL"
    assert body["format_validation"]["invalid_paths"] == ["invoiceStatus"]
    assert body["data"]["invoiceStatus"] is None


# ------------------------------------------------------------------ image prep
def test_extraction_is_fed_the_prepared_bytes_not_the_file(tmp_path, monkeypatch):
    """What the app sends is the quality stage's clean image. The harness must send the same
    thing, through `extract_from_buffer` -- `extract(path)` would re-read the source."""
    import io

    from PIL import Image

    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")

    image = tmp_path / "Template1_Instance1.png"
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), (10, 120, 200)).save(buf, format="PNG")
    image.write_bytes(buf.getvalue())

    seen = {}

    class FakeResult:
        data = {"invoiceInfo": {}}
        metadata = {"input": 1}

    class FakeSvc:
        def extract_from_buffer(self, file_buffer, mime_type, **kw):
            seen["bytes"], seen["mime"], seen["kw"] = file_buffer, mime_type, kw
            return FakeResult()

        def extract(self, *a, **k):                     # pragma: no cover - must not be called
            raise AssertionError("extract(path) bypasses the quality-stage transform")

    runner._svc = FakeSvc()
    raw, meta = runner._extract(R.DocResult(doc_id="d"), image, "d")

    expected, expected_mime = pc.prepare_extraction_image(image.read_bytes())
    assert seen["bytes"] == expected and seen["mime"] == expected_mime == "image/jpeg"
    assert seen["bytes"] != image.read_bytes()
    assert seen["kw"]["part"] == "all"
    assert seen["kw"]["job_id"] is None, "a job_id re-enables the Redis thinking stream"
    assert meta["image_prep"]["version"] == pc.IMAGE_PREP_VERSION
    assert meta["image_prep"]["quality_transform"] is True
    assert raw == {"invoiceInfo": {}}


def test_source_bytes_mode_is_selectable_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(image_prep="source_bytes"), ["RAW"], "t")
    assert runner.image_prep == "source_bytes" and runner.quality_transform is False


def test_an_unknown_image_prep_mode_is_refused(tmp_path, monkeypatch):
    """Silently defaulting would leave a run's most consequential input unrecorded."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    with pytest.raises(ValueError, match="image_prep"):
        R.Runner(_cfg(image_prep="original_png"), ["RAW"], "t")


def test_the_cache_key_separates_the_two_image_prep_modes(tmp_path, monkeypatch):
    """A cached extraction is keyed to the bytes that produced it. Without the prep term, a
    re-run after switching modes would serve verdicts computed from an input no longer sent."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    image = tmp_path / "d.png"
    image.write_bytes(b"whatever -- only its sha256 reaches the key")

    jpeg = R.Runner(_cfg(), ["RAW"], "t")._cache_key(image, "extract")
    src = R.Runner(_cfg(image_prep="source_bytes"), ["RAW"], "t")._cache_key(image, "extract")
    assert jpeg != src
    assert pc.IMAGE_PREP_VERSION in jpeg


def test_the_manifest_states_what_input_was_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    m = runner.manifest([], {"doc_ids": [], "_path": "x"})
    assert m["image_prep"] == {"mode": "quality_clean_jpeg", "version": pc.IMAGE_PREP_VERSION}
    assert m["tail_version"] == pc.TAIL_VERSION


def test_image_prep_reaches_the_runner_from_the_command_line(monkeypatch, tmp_path):
    """A controlled JPEG-vs-source pair should not need a config edit between the two runs --
    an edited config is recorded nowhere, so afterwards the two runs are indistinguishable
    except by their manifests. The flag has to land in cfg before the Runner is built."""
    class Stop(Exception):
        pass

    built = {}
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_env", lambda: [])
    monkeypatch.setattr(R, "load", lambda: {**_cfg(),
                                            "paths": {"ai_backend": str(tmp_path),
                                                      "dataset": str(tmp_path)}})

    # main() reads the plan straight after applying the override. Capture cfg there.
    real_loads = R.json.loads

    def capture(text, *a, **k):
        built["mode"] = R.load()["run"].get("image_prep")
        raise Stop()

    monkeypatch.setattr(R.json, "loads", capture)
    plan = tmp_path / "plan.json"
    plan.write_text("{}")

    with pytest.raises(Stop):
        R.main(["--plan", str(plan), "--image-prep", "source_bytes"])
    monkeypatch.setattr(R.json, "loads", real_loads)

    # load() is stubbed to return a fresh dict, so the captured value proves only that main
    # got that far. The override itself is asserted directly on the Runner, which is where it
    # has to take effect.
    cfg = _cfg()
    cfg["run"]["image_prep"] = "source_bytes"
    assert R.Runner(cfg, ["RAW"], "t").quality_transform is False


def test_an_unknown_image_prep_flag_is_rejected_by_the_parser():
    """Better a usage error than a run that silently measures the default input."""
    with pytest.raises(SystemExit):
        R.main(["--plan", "x.json", "--image-prep", "original_png"])
