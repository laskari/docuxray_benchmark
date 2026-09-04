"""The runner must never freeze, and must never lose a stage it already paid for.

These are regression tests for the 2026-09-02 hang: a 10-document run in which three
documents stalled inside a single judge section (Template10 and Template16 on `totals`,
Template14 on `invoiceInfo`) while their other four sections came back in seconds. Nothing
timed out, because ai/llm sets no HTTP read timeout and the judge's own fan-out waits on
as_completed with no timeout, so the run sat there until it was killed -- and every completed
extraction for those three documents was thrown away with it.

No network, no ai_backend, no API key: the stages are stubbed, because what is under test is
the harness's control flow, not the model's output.
"""
from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest

from core import runner as R


# --------------------------------------------------------------------------- _run_with_timeout
def test_returns_value_and_propagates_exceptions():
    assert R._run_with_timeout(lambda: 41 + 1, 5, "ok") == 42
    with pytest.raises(ValueError, match="boom"):
        R._run_with_timeout(lambda: (_ for _ in ()).throw(ValueError("boom")), 5, "err")


def test_stalled_call_raises_stage_timeout_and_does_not_block():
    """The one thing the old code could not do: give up."""
    release = threading.Event()
    t0 = time.time()
    with pytest.raises(R.StageTimeout):
        R._run_with_timeout(lambda: release.wait(60), 0.3, "doc:judge")
    assert time.time() - t0 < 5, "gave up late -- it waited on the stalled call"
    release.set()


def test_abandoned_stage_thread_is_a_daemon():
    """A ThreadPoolExecutor worker would keep the interpreter alive at exit via the atexit
    hook concurrent.futures installs. That is what turned one stalled section into a process
    that would not die."""
    release = threading.Event()
    with pytest.raises(R.StageTimeout):
        R._run_with_timeout(lambda: release.wait(60), 0.2, "doc:judge")
    stage_threads = [t for t in threading.enumerate() if t.name.startswith("stage-")]
    assert stage_threads, "expected the abandoned thread to still be running"
    assert all(t.daemon for t in stage_threads)
    release.set()


def test_leak_is_recorded():
    before = len(R._LEAKED)
    release = threading.Event()
    with pytest.raises(R.StageTimeout):
        R._run_with_timeout(lambda: release.wait(60), 0.2, "TemplateX:judge")
    assert "TemplateX:judge" in R._LEAKED[before:]
    release.set()


# --------------------------------------------------------------------------- fixtures
def _cfg():
    return {
        "run": {"doc_type": "invoice", "spend_cap_usd": 250.0, "concurrency": 2,
                "early_abort_after": 0,
                "stage_timeouts": {"extract": 0.4, "judge": 0.4, "free": 0.4}},
        "cache": {"enabled": False, "dir": "_cache"},
        "models": {"extraction": "test-extract", "judge": "test-judge"},
    }


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)          # keep runs/ out of the repo
    # extraction_postprocessing is production code from ai_backend; stub it everywhere.
    monkeypatch.setattr(R.Runner, "_extraction_postprocessing", lambda self, raw: raw)
    return R.Runner(_cfg(), ["RAW"], "t")


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "Template1_Instance1.png"
    p.write_bytes(b"not really a png, but it hashes")
    return p


# --------------------------------------------------------------------------- intermediates
def test_extraction_is_on_disk_before_anything_else_runs(runner, image, monkeypatch):
    monkeypatch.setattr(R.Runner, "_extract",
                        lambda self, r, img, doc: ({"invoice": {"total": "10"}}, {"input": 7}))
    r = runner.run_document("Template1_Instance1", str(image))

    d = runner.stages_dir / "Template1_Instance1"
    assert r.ok
    assert r.stage_status == {"extract": "ok", "extraction_postprocessing": "ok"}
    body = json.loads((d / "01_extract_raw.json").read_text())
    # bare extraction is an intermediate now: kept on disk, but it is not an arm
    assert body["arm"] is None and body["data"] == {"invoice": {"total": "10"}}
    assert body["model"] == "test-extract"

    raw_arm = json.loads((d / "02_extraction_postprocessing.json").read_text())
    assert raw_arm["arm"] == "RAW"
    assert r.arms == {"RAW": {"invoice": {"total": "10"}}}

    status = json.loads((d / "00_status.json").read_text())
    assert status["finished"] and status["ok"] and not status["timed_out"]
    assert status["files"] == {"extract": "01_extract_raw.json",
                               "extraction_postprocessing":
                                   "02_extraction_postprocessing.json"}


def test_a_stalled_stage_keeps_the_stages_that_already_finished(tmp_path, monkeypatch, image):
    """The point of the whole change. Extraction is paid for; a judge that never returns must
    not take it down with it."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    monkeypatch.setattr(R.Runner, "_extract",
                        lambda self, r, img, doc: ({"invoice": {"total": "10"}}, {"input": 7}))
    release = threading.Event()
    monkeypatch.setattr(R.Runner, "_extraction_postprocessing",
                        lambda self, raw: release.wait(60))

    r = runner.run_document("Template1_Instance1", str(image))
    release.set()

    assert not r.ok and r.timed_out
    assert r.error_stage == "extraction_postprocessing"
    assert r.stage_status == {"extract": "ok", "extraction_postprocessing": "timeout"}
    d = runner.stages_dir / "Template1_Instance1"
    assert (d / "01_extract_raw.json").exists(), "the paid-for extraction was lost again"
    assert not (d / "02_extraction_postprocessing.json").exists()
    status = json.loads((d / "00_status.json").read_text())
    assert status["timed_out"] and status["stage_status"]["extraction_postprocessing"] == "timeout"


def test_stage_file_names_are_stable():
    """step6/step7 and any downstream reader address these by name."""
    assert R.STAGE_FILES == {
        "extract":                   "01_extract_raw.json",
        "extraction_postprocessing": "02_extraction_postprocessing.json",
        "judge":                     "03_judge_report.json",
        "refine":                    "04_refined.json",
        "postprocess":               "05_postprocessed.json",
    }


# --------------------------------------------------------------------------- the run itself
def test_one_stalled_document_does_not_stall_the_run(tmp_path, monkeypatch):
    """Three of ten documents hung and took the other seven's run with them. Not any more."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R.Runner, "_extraction_postprocessing", lambda self, raw: raw)
    runner = R.Runner(_cfg(), ["RAW"], "t")

    images = {}
    for i in range(5):
        p = tmp_path / f"doc{i}.png"
        p.write_bytes(f"image {i}".encode())
        images[f"doc{i}"] = str(p)
    release = threading.Event()

    def fake_extract(self, r, img, doc):
        if doc == "doc2":
            release.wait(60)                       # the stalled judge section, in miniature
        return {"invoice": {"id": doc}}, {"input": 1, "cost": {"total_cost": 0.01}}

    monkeypatch.setattr(R.Runner, "_extract", fake_extract)

    t0 = time.time()
    results = runner.run_plan(list(images), images)
    elapsed = time.time() - t0
    release.set()

    assert elapsed < 20, f"the run did not move on ({elapsed:.0f}s)"
    assert len(results) == 5
    by_id = {r.doc_id: r for r in results}
    assert sum(1 for r in results if r.ok) == 4
    assert by_id["doc2"].timed_out and by_id["doc2"].stage_status["extract"] == "timeout"
    # every document, stalled or not, leaves a raw/*.json so --resume can retry it
    assert {p.stem for p in (runner.out / "raw").glob("*.json")} == set(images)

    m = runner.manifest(results, {"doc_ids": list(images), "_path": "x"})
    assert m["n_ok"] == 4 and m["n_timed_out"] == 1
    assert m["stage_failures"] == {"extract:timeout": 1}
    assert m["stage_timeouts_s"]["judge"] == 0.4


def test_per_doc_budget_covers_the_stages_it_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    assert R.Runner(_cfg(), ["RAW"], "t").per_doc_budget() == pytest.approx(0.4 + 3 * 0.4)
    assert R.Runner(_cfg(), ["RAW", "FINAL"], "t").per_doc_budget() == \
        pytest.approx(0.4 + 0.4 + 3 * 0.4)


def test_old_arm_names_are_rejected_with_a_migration_hint(tmp_path, monkeypatch):
    """A/B/C/D silently scoring as unknown arms would produce an empty report, not an error."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    with pytest.raises(ValueError) as e:
        R.Runner(_cfg(), ["A", "B", "C", "D"], "t")
    assert "B -> RAW" in str(e.value) and "D -> FINAL" in str(e.value)
    with pytest.raises(ValueError):
        R.Runner(_cfg(), [], "t")


def test_arm_names_are_normalised(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    assert R.Runner(_cfg(), [" raw ", "final"], "t").arms == ["RAW", "FINAL"]


def test_cli_timeout_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    r = R.Runner(_cfg(), ["RAW"], "t", stage_timeouts={"judge": 900, "extract": None})
    assert r.stage_timeouts["judge"] == 900
    assert r.stage_timeouts["extract"] == 0.4      # None must not clobber the config value
