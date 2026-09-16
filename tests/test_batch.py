"""The batch pre-pass must produce a run indistinguishable from a synchronous one.

Batch mode does not change what is measured -- same model, same prompts, same prepared
bytes -- only how the bytes are fetched. Everything here is a pin on that claim, plus on the
two places where batch genuinely differs and must SAY so: retries, and price.

No network and no API key. The Gemini provider is a fake whose `create_batch` records what it
was handed and whose `get_batch_results` replays a scripted answer, so these tests exercise
the harness's control flow -- chunking, resume, routing, fallback, accounting -- rather than
the model's output. The schemas, prompts and postprocessors are the REAL ai_backend ones
wherever they do not need a key, because the point of the pre-pass is that it calls the
product's own request builders.
"""
from __future__ import annotations

import io
import json
import pathlib

import pytest

from core import batch as B
from core import runner as R

pc = pytest.importorskip("ai.pipeline_core",
                         reason="needs ai_backend on PYTHONPATH (source activate.env)")


# --------------------------------------------------------------------------------- fixtures
def _cfg(**run_overrides):
    run = {"doc_type": "receipt", "dataset": "sroie", "spend_cap_usd": 250.0,
           "concurrency": 2, "early_abort_after": 0,
           "stage_timeouts": {"extract": 5, "judge": 5, "free": 5},
           "batch": {"poll_interval_s": 0, "job_timeout_s": 5,
                     "max_requests_per_job": 200, "max_mb_per_job": 18}}
    run.update(run_overrides)
    return {"run": run, "cache": {"enabled": True, "dir": "_cache"},
            "models": {"extraction": "test-extract", "judge": "test-judge"}}


def _png(path: pathlib.Path, colour=(10, 120, 200)) -> pathlib.Path:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), colour).save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


def _noisy_png(path: pathlib.Path) -> pathlib.Path:
    """An image JPEG re-encoding measurably changes — flat colours round-trip exactly and so
    cannot tell the prepared bytes from the source."""
    from PIL import Image
    px = bytes((i * 37 + j * 91) % 256 for i in range(64) for j in range(64) for _ in (0,))
    img = Image.frombytes("L", (64, 64), px).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    path.write_bytes(buf.getvalue())
    return path


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    return R.Runner(_cfg(), ["RAW"], "t")


# --------------------------------------------------------------------------- fake provider
class FakeJob:
    def __init__(self, name, keys, state="JOB_STATE_SUCCEEDED"):
        self.name = name
        self.state = state
        self.error = None
        self._docuxray_keys = keys
        self.dest = None


class FakeProvider:
    """Records submissions; replays `answers[key] -> (text, error)`."""

    def __init__(self, answers, state="JOB_STATE_SUCCEEDED"):
        self.answers = answers
        self.state = state
        self.submitted = []          # [(display_name, [keys])]
        self.jobs = {}

    def create_batch(self, requests_map, *, display_name="x"):
        keys = list(requests_map)
        name = f"batches/job{len(self.submitted)}"
        self.submitted.append((display_name, keys))
        job = FakeJob(name, keys, self.state)
        self.jobs[name] = job
        return job

    def retrieve_batch(self, name):
        return self.jobs[name]

    def get_batch_results(self, job):
        for key in getattr(job, "_docuxray_keys", []):
            text, error = self.answers.get(key, ("{}", None))
            yield {"key": key, "error": error, "response_text": text,
                   "token_usage": {"input": 100, "output": 20, "thinking": 5, "total": 125}}


def _prepass(runner, provider, **kw):
    pre = B.BatchPrepass(runner, _cfg(), log=lambda *_: None, **kw)
    pre._provider = provider
    return pre


def _run_prepass_with(monkeypatch, runner, provider, doc_ids, images, stages):
    """Drive the real run_prepass, with the fake provider injected into whatever
    BatchPrepass it builds."""
    real = B.BatchPrepass

    def factory(*a, **k):
        pre = real(*a, **k)
        pre._provider = provider
        return pre

    monkeypatch.setattr(B, "BatchPrepass", factory)
    return B.run_prepass(runner, _cfg(), doc_ids, images, stages=stages,
                         log=lambda *_: None)


# ------------------------------------------------------------------------------- unit: cost
def test_discount_scales_every_component_and_keeps_the_list_price():
    """A reader summing the components must get the total, and a benchmark that hides what
    the interactive price WOULD have been cannot answer 'what does this cost in production'."""
    cost = {"model": "m", "input_cost": 1.0, "output_cost": 2.0, "thinking_cost": 1.0,
            "total_cost": 4.0}
    out = B._scale_cost(cost, 0.5)
    assert out["total_cost"] == 2.0
    assert out["input_cost"] + out["output_cost"] + out["thinking_cost"] == out["total_cost"]
    assert out["list_price_total_cost"] == 4.0
    assert out["batch_discount"] == 0.5


def test_tokens_are_totalled_the_way_the_sync_path_totals_them():
    """`get_batch_results` reports candidates-only output; the API's own total_token_count
    counts thinking differently. Recomputing keeps a batch document comparable with a sync
    one."""
    acc = {}
    B._merge_tokens(acc, {"input": 10, "output": 3, "thinking": 2, "total": 999})
    B._merge_tokens(acc, {"input": 5, "output": 1, "thinking": 1, "total": 999})
    assert acc == {"input": 15, "output": 4, "thinking": 3, "total": 22}


# ------------------------------------------------------------------------------- unit: jobs
def _driver(tmp_path, provider, **kw):
    kw.setdefault("poll_interval", 0)
    kw.setdefault("timeout", 5)
    kw.setdefault("max_requests", 200)
    kw.setdefault("max_bytes", 18.0)
    return B._JobDriver(provider, tmp_path / "s.json", "extract", log=lambda *_: None, **kw)


def test_jobs_split_on_request_count(tmp_path):
    d = _driver(tmp_path, None, max_requests=2)
    reqs = {f"k{i}": object() for i in range(5)}
    assert [len(c) for c in d._chunks(reqs, {})] == [2, 2, 1]


def test_jobs_split_on_payload_size(tmp_path):
    """Inlined requests ride in the create call, so a job is bounded by bytes, not count."""
    d = _driver(tmp_path, None, max_bytes=1.0)          # 1MB
    reqs = {f"k{i}": object() for i in range(4)}
    sizes = {k: 400 * 1024 for k in reqs}               # 0.4MB each -> 2 per job
    assert [len(c) for c in d._chunks(reqs, sizes)] == [2, 2]


def test_a_single_oversized_request_is_not_dropped(tmp_path):
    """It will probably fail at the API, but silently discarding it would lose a document
    with no trace."""
    d = _driver(tmp_path, None, max_bytes=1.0)
    assert d._chunks({"big": object()}, {"big": 50 * 1024 * 1024}) == [["big"]]


def test_the_job_name_is_on_disk_before_the_first_poll(tmp_path):
    """The tokens are committed at submit time. A run killed during the wait must be able to
    find the job it already paid for."""
    seen = {}

    class Recording(FakeProvider):
        def retrieve_batch(self, name):
            seen["state_file"] = (tmp_path / "s.json").exists()
            return super().retrieve_batch(name)

    p = Recording({"a": ("{}", None)})
    list(_driver(tmp_path, p).run({"a": object()}, {}, "disp"))
    assert seen["state_file"] is True
    assert json.loads((tmp_path / "s.json").read_text())["chunks"][0]["job"] == "batches/job0"


def test_resume_collects_the_submitted_job_instead_of_paying_again(tmp_path):
    p = FakeProvider({"a": ('{"x": 1}', None)})
    d = _driver(tmp_path, p)
    list(d.run({"a": object()}, {}, "disp"))
    assert len(p.submitted) == 1

    again = list(d.run({"a": object()}, {}, "disp", resume=True))
    assert len(p.submitted) == 1, "resume re-submitted a job that was already paid for"
    assert [r["key"] for r in again] == ["a"]


def test_resume_restores_the_keys_a_refetched_job_has_lost(tmp_path):
    """`create_batch` stamps submission order onto the object it returns; a job fetched back
    by name has not got it, and results would come back keyed request_0, request_1..."""
    p = FakeProvider({"a": ("{}", None), "b": ("{}", None)})
    d = _driver(tmp_path, p)
    list(d.run({"a": object(), "b": object()}, {}, "disp"))
    p.jobs["batches/job0"]._docuxray_keys = None            # simulate the refetch
    assert [r["key"] for r in d.run({}, {}, "disp", resume=True)] == ["a", "b"]


def test_a_failed_job_aborts_rather_than_scoring_nothing(tmp_path):
    p = FakeProvider({"a": ("{}", None)}, state="JOB_STATE_FAILED")
    with pytest.raises(B.BatchAborted, match="JOB_STATE_FAILED"):
        list(_driver(tmp_path, p).run({"a": object()}, {}, "disp"))


def test_a_job_that_never_finishes_says_how_to_resume(tmp_path):
    class Stuck(FakeProvider):
        def retrieve_batch(self, name):
            job = super().retrieve_batch(name)
            job.state = "JOB_STATE_RUNNING"
            return job

    p = Stuck({"a": ("{}", None)})
    with pytest.raises(B.BatchAborted, match="--resume"):
        list(_driver(tmp_path, p, timeout=0.05).run({"a": object()}, {}, "disp"))


def test_batch_mode_refuses_to_run_without_a_cache(tmp_path, monkeypatch):
    """The pre-pass writes results into the cache and the run reads them back. With the cache
    off there is nowhere to put them and every document would be paid for twice."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    cfg = _cfg()
    cfg["cache"]["enabled"] = False
    with pytest.raises(ValueError, match="cache.enabled"):
        B.BatchPrepass(R.Runner(cfg, ["RAW"], "t"), cfg)


# -------------------------------------------------------------------------------- extraction
class FakeSvc:
    """Stands in for GeminiExtractionService: the same two entry points the pre-pass uses."""

    def __init__(self, parts=("parties", "totals"), fail_sync=False):
        from ai.extraction.new_schema import RECEIPT_SCHEMA_MAP, ReceiptData
        self._schema_map = RECEIPT_SCHEMA_MAP
        self._root_schema = ReceiptData
        self._parts = list(parts)
        self.fail_sync = fail_sync
        self.batch_images = []
        self.sync_calls = []

    def generate_batch_requests(self, image, part="all"):
        self.batch_images.append(image)
        for p in self._parts:
            yield p, object()

    def extract_from_buffer(self, buf, mime, part=None, document_id=None, job_id=None):
        self.sync_calls.append((document_id, part, mime, len(buf)))
        if self.fail_sync:
            raise RuntimeError("sync retry also failed")

        class Res:
            data = {"totals": {"totalIncludingTax": {"originalValue": "9.99"}}}
            metadata = {"prompt_tokens": 7, "output_tokens": 2, "thinking_tokens": 1,
                        "total_tokens": 10, "attempts": 2,
                        "cost": {"total_cost": 0.001}}
        return Res()


def _answers_for(doc_ids, parts, body='{}'):
    return {f"{d}{B.SEP}{p}": (body, None) for d in doc_ids for p in parts}


def test_extraction_fills_the_cache_the_runner_then_reads(tmp_path, monkeypatch):
    """The whole design in one test: the pre-pass writes under `_cache_key`, and
    `run_document` finds it without knowing a batch ever happened."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("parties", "totals"))
    img = _png(tmp_path / "d1.png")
    images = {"d1": str(img)}

    p = FakeProvider(_answers_for(["d1"], ["parties", "totals"]))
    st = _prepass(runner, p).extract(["d1"], images)

    assert st.batched == 1 and st.filled == ["d1"] and st.fell_through == 0
    hit = runner._cache_get(runner._cache_key(img, "extract"))
    assert hit is not None, "the pre-pass did not write the key the runner reads"

    r = R.DocResult(doc_id="d1")
    runner._batch_filled = {"extract": {"d1"}}
    raw, meta = runner._extract(r, img, "d1")
    assert meta["batch"]["mode"] == "gemini_batch_api"
    assert meta["image_prep"]["version"] == pc.IMAGE_PREP_VERSION
    assert meta["image_prep"]["quality_transform"] is True


def test_the_extractor_is_handed_the_prepared_bytes_not_the_file(tmp_path, monkeypatch):
    """The cache key already asserts which bytes were sent. A batch request built off the
    source PNG would be filed under a key describing pixels it never saw."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    svc = FakeSvc(parts=("totals",))
    runner._svc = svc
    img = _noisy_png(tmp_path / "d1.png")

    _prepass(runner, FakeProvider(_answers_for(["d1"], ["totals"]))).extract(
        ["d1"], {"d1": str(img)})

    from PIL import Image
    expected, _ = pc.prepare_extraction_image(img.read_bytes())
    prepared_px = list(Image.open(io.BytesIO(expected)).convert("RGB").getdata())
    source_px = list(Image.open(img).convert("RGB").getdata())
    sent = list(svc.batch_images[0].convert("RGB").getdata())
    assert prepared_px != source_px, "fixture does not separate the two paths"
    assert sent == prepared_px and sent != source_px


def test_an_already_cached_document_is_not_submitted(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",))
    img = _png(tmp_path / "d1.png")
    runner._cache_put(runner._cache_key(img, "extract"), {"data": {}, "metadata": {}})

    p = FakeProvider({})
    st = _prepass(runner, p).extract(["d1"], {"d1": str(img)})
    assert p.submitted == [] and st.requested == 0


def test_a_broken_part_is_retried_synchronously_at_list_price(tmp_path, monkeypatch):
    """Sync extraction retries a part three times inside `_run_parts`; a batch request gets
    one shot. Accepting the broken part would score a null the product would have re-asked
    for."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    svc = FakeSvc(parts=("parties", "totals"))
    runner._svc = svc
    img = _png(tmp_path / "d1.png")

    answers = _answers_for(["d1"], ["parties"])
    answers[f"d1{B.SEP}totals"] = ("not json at all {{{", None)
    st = _prepass(runner, FakeProvider(answers)).extract(["d1"], {"d1": str(img)})

    assert st.batched == 1 and st.sync_retried == 1
    assert [c[1] for c in svc.sync_calls] == ["totals"], "retried the wrong part"
    meta = runner._cache_get(runner._cache_key(img, "extract"))["metadata"]
    assert meta["batch"]["parts_from_sync_retry"] == ["totals"]
    assert meta["batch"]["parts_from_batch"] == ["parties"], (
        "a part whose batch answer was unparseable is not a part the batch supplied")
    # ... but the batch still generated (and billed) those tokens, so they stay in the bill.
    assert meta["per_part"][1]["failed"] is True
    # The retry was interactive, so it is charged at list price on top of the discount.
    assert meta["cost"]["sync_retry_cost"] == pytest.approx(0.001)


def test_an_api_error_on_a_part_also_routes_to_the_sync_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    svc = FakeSvc(parts=("totals",))
    runner._svc = svc
    img = _png(tmp_path / "d1.png")

    st = _prepass(runner, FakeProvider(
        {f"d1{B.SEP}totals": (None, "RESOURCE_EXHAUSTED")})).extract(["d1"], {"d1": str(img)})
    assert st.sync_retried == 1 and st.batched == 1


def test_a_document_the_batch_cannot_complete_is_left_to_the_run(tmp_path, monkeypatch):
    """Not cached at all, deliberately: a partial extraction in the cache would be read back
    as a complete one for the rest of this run and every run after it."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("parties", "totals"), fail_sync=True)
    img = _png(tmp_path / "d1.png")

    answers = _answers_for(["d1"], ["parties"])
    answers[f"d1{B.SEP}totals"] = ("{", None)
    st = _prepass(runner, FakeProvider(answers)).extract(["d1"], {"d1": str(img)})

    assert st.batched == 0 and st.fell_through == 1
    assert runner._cache_get(runner._cache_key(img, "extract")) is None


def test_an_undecodable_image_does_not_take_the_batch_down_with_it(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",))
    good = _png(tmp_path / "good.png")
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")

    p = FakeProvider(_answers_for(["good"], ["totals"]))
    st = _prepass(runner, p).extract(["good", "bad"],
                                    {"good": str(good), "bad": str(bad)})
    assert st.batched == 1 and st.fell_through == 1
    assert all("bad" not in k for _, keys in p.submitted for k in keys)


def test_results_are_routed_by_key_not_by_arrival_order(tmp_path, monkeypatch):
    """Two documents, answers deliberately distinguishable, replayed in reverse."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",))
    a, b = _png(tmp_path / "a.png", (1, 2, 3)), _png(tmp_path / "b.png", (9, 9, 9))

    class Reversed(FakeProvider):
        def get_batch_results(self, job):
            yield from reversed(list(super().get_batch_results(job)))

    p = Reversed({
        f"a{B.SEP}totals": ('{"totals": {"totalIncludingTax": {"originalValue": "1.00"}}}',
                            None),
        f"b{B.SEP}totals": ('{"totals": {"totalIncludingTax": {"originalValue": "2.00"}}}',
                            None),
    })
    _prepass(runner, p).extract(["a", "b"], {"a": str(a), "b": str(b)})

    got_a = runner._cache_get(runner._cache_key(a, "extract"))["data"]
    got_b = runner._cache_get(runner._cache_key(b, "extract"))["data"]
    assert got_a["totals"]["totalIncludingTax"]["originalValue"] == "1.00"
    assert got_b["totals"]["totalIncludingTax"]["originalValue"] == "2.00"


# ------------------------------------------------------------------------------------ judge
class FakeJudge:
    model_name = "test-judge"

    def _build_section_verification_prompt(self, prompt_text, section_data):
        return f"{prompt_text}||{json.dumps(section_data, sort_keys=True, default=str)}"


_SECTION_OK = '{"issues": [], "confidence": 0.9}'
_SECTION_LOW = '{"issues": [], "confidence": 0.4}'


def _seed_extraction(runner, doc_id, image, data):
    runner._cache_put(runner._cache_key(image, "extract"),
                      {"data": data, "metadata": {"cost": {"total_cost": 0.0}}})


def _receipt(total="9.99"):
    return {"parties": {"seller": {"name": "Acme"}},
            "totals": {"totalIncludingTax": {"originalValue": total}}}


def _judge_prepass(runner, answers):
    runner._judge = FakeJudge()
    return _prepass(runner, FakeProvider(answers))


def _sections_present(runner, image, doc_id="d1"):
    """The sections the pre-pass will actually send, derived the way it derives them."""
    from ai.judge.core.judge_prompts import get_section_name, get_section_prompts
    hit = runner._cache_get(runner._cache_key(image, "extract"))
    raw_pp = runner._extraction_postprocessing(hit["data"])
    wrapper = pc.WRAPPER_KEYS.get(runner.doc_type)
    unwrapped = raw_pp.get(wrapper, raw_pp) if wrapper else raw_pp
    names = []
    for sp in get_section_prompts(runner.doc_type):
        name = get_section_name(sp)
        if unwrapped.get(name) is not None:
            names.append(name)
    return names, unwrapped


def test_judge_caches_under_the_key_verify_would_have_used(tmp_path, monkeypatch):
    """The judge key hashes the data being verified. If the pre-pass derives that input even
    slightly differently, every entry it writes is a permanent miss."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())

    names, unwrapped = _sections_present(runner, img)
    assert names, "fixture produced no verifiable section"
    answers = {f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names}
    st = _judge_prepass(runner, answers).judge(["d1"], {"d1": str(img)})

    assert st.batched == 1
    assert runner._cache_get(runner._cache_key(img, "judge", unwrapped)) is not None


def test_the_judge_is_sent_the_source_image_the_sync_path_sends(tmp_path, monkeypatch):
    """`_verify` calls verify_sectional with `Image.open(image)` -- the file on disk, NOT
    prepare_extraction_image's output. The judge and the extractor see different pixels, and
    the judge's cache key does not cover the difference."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _noisy_png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())
    names, _ = _sections_present(runner, img)

    seen = []

    class Capturing(FakeProvider):
        def create_batch(self, requests_map, *, display_name="x"):
            seen.extend(r.contents[0] for r in requests_map.values())
            return super().create_batch(requests_map, display_name=display_name)

    pre = _prepass(runner, Capturing({f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names}))
    runner._judge = FakeJudge()
    pre.judge(["d1"], {"d1": str(img)})

    from PIL import Image
    source = list(Image.open(img).convert("RGB").getdata())
    prepared, _ = pc.prepare_extraction_image(img.read_bytes())
    prepared_px = list(Image.open(io.BytesIO(prepared)).convert("RGB").getdata())
    sent = list(seen[0].convert("RGB").getdata())
    assert prepared_px != source, (
        "fixture does not separate the two paths — pick an image JPEG actually degrades")
    assert sent == source, "the judge was sent the extractor's prepared bytes"


def test_aggregate_confidence_is_the_minimum_over_sections(tmp_path, monkeypatch):
    """verify_sectional uses min(), not mean(). step7 reads this number."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())
    names, unwrapped = _sections_present(runner, img)

    answers = {f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names}
    answers[f"d1{B.SEP}{names[0]}"] = (_SECTION_LOW, None)
    _judge_prepass(runner, answers).judge(["d1"], {"d1": str(img)})

    report = runner._cache_get(runner._cache_key(img, "judge", unwrapped))
    assert report["confidence"] == pytest.approx(0.4)


def test_issues_are_keyed_by_section_so_refinement_can_apply_them(tmp_path, monkeypatch):
    """`run_refinement_pipeline` reads `issues` out of this report and nothing else."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())
    names, unwrapped = _sections_present(runner, img)

    issue = ('{"issues": [{"field": "totalIncludingTax", "issue_type": "numeric_value_mismatch", '
             '"reasoning": "r", "description": "d", "extracted_value": "9.99", '
             '"corrected_value": "10.99", "expected_context": null}], "confidence": 0.7}')
    answers = {f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names}
    answers[f"d1{B.SEP}totals"] = (issue, None)
    _judge_prepass(runner, answers).judge(["d1"], {"d1": str(img)})

    report = runner._cache_get(runner._cache_key(img, "judge", unwrapped))
    assert report["total_issues"] == 1
    assert report["judge_passed"] is False
    assert report["issues"]["totals"][0]["corrected_value"] == "10.99"

    from ai.refinement.pipeline import run_refinement_pipeline
    refined = run_refinement_pipeline(
        runner._extraction_postprocessing(_receipt()), report, "receipt")
    assert refined["refined_data"] is not None


def test_one_failed_section_drops_the_whole_document(tmp_path, monkeypatch):
    """Confidence is the MINIMUM over sections, so a report missing a section is not slightly
    incomplete -- it is a different, more confident report. Re-judge it whole."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())
    names, unwrapped = _sections_present(runner, img)

    answers = {f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names}
    answers[f"d1{B.SEP}{names[0]}"] = (None, "INTERNAL")
    st = _judge_prepass(runner, answers).judge(["d1"], {"d1": str(img)})

    assert st.batched == 0 and st.fell_through == 1
    assert runner._cache_get(runner._cache_key(img, "judge", unwrapped)) is None


def test_judge_skips_a_document_whose_extraction_never_landed(tmp_path, monkeypatch):
    """Its judge key does not exist yet -- the key hashes RAW, and RAW is not there."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    img = _png(tmp_path / "d1.png")
    p = FakeProvider({})
    runner._judge = FakeJudge()
    st = _prepass(runner, p).judge(["d1"], {"d1": str(img)})
    assert st.fell_through == 1 and p.submitted == []


# ------------------------------------------------------------------------------ accounting
def test_a_batch_run_does_not_report_itself_as_a_free_cached_run(tmp_path, monkeypatch):
    """A pre-pass hit IS a cache hit by the time run_document reads it. Counting it as one
    would report a run that just spent money as one that spent none."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._batch_filled = {"extract": {"fresh"}}

    fresh, old = R.DocResult(doc_id="fresh"), R.DocResult(doc_id="old")
    runner._mark_hit(fresh, "extract")
    runner._mark_hit(old, "extract")

    assert fresh.batch_stages == {"extract": True} and fresh.cached_stages["extract"] is False
    assert old.batch_stages == {} and old.cached_stages["extract"] is True

    m = runner.manifest([fresh, old], {"doc_ids": [], "_path": "x"})
    assert m["n_from_batch"] == 1


def test_the_stage_status_says_batch_rather_than_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "_run_with_timeout", lambda fn, t, label: fn())
    runner = R.Runner(_cfg(), ["RAW"], "t")
    r = R.DocResult(doc_id="d1")
    r.batch_stages["extract"] = True
    runner._stage(r, "extract", lambda: None)
    assert r.stage_status["extract"] == "batch"


def test_the_spend_cap_is_checked_before_the_job_is_submitted(tmp_path, monkeypatch):
    """`run_plan`'s cap runs per document, after each is paid for. One create_batch commits
    the whole plan at once, so the check has to happen before it."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    cfg = _cfg(spend_cap_usd=0.01)
    runner = R.Runner(cfg, ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",))
    img = _png(tmp_path / "d1.png")
    # Seed a measured cost so the projection is a real number rather than a shrug.
    runner._cache_put("seed.test-extract.frozen.x.extract",
                      {"data": {}, "metadata": {"cost": {"total_cost": 5.0}}})

    p = FakeProvider(_answers_for(["d1"], ["totals"]))
    pre = _prepass(runner, p)
    with pytest.raises(B.BatchAborted, match="cap"):
        pre.extract(["d1"], {"d1": str(img)})
    assert p.submitted == [], "money was committed before the cap was checked"


def test_the_manifest_records_how_the_bytes_were_fetched(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",))
    img = _png(tmp_path / "d1.png")
    pre = _prepass(runner, FakeProvider(_answers_for(["d1"], ["totals"])))
    pre.extract(["d1"], {"d1": str(img)})

    block = pre.manifest_block()
    assert block["mode"] == "batch" and block["discount"] == 0.5
    stage = block["stages"][0]
    assert stage["stage"] == "extract" and stage["documents_from_batch"] == 1
    assert stage["jobs"] == ["batches/job0"]


# ------------------------------------------------------------------------------------- CLI
def test_mode_batch_reaches_the_runner_from_the_command_line(monkeypatch, tmp_path):
    """An edited config is recorded nowhere. A sync/batch pair has to be selectable per
    invocation or the two runs are indistinguishable afterwards."""
    class Stop(Exception):
        pass

    # ONE shared dict, so the override main() applies is visible afterwards. A fresh dict
    # per call would only prove main() reached the plan read, which is not the claim.
    cfg = {**_cfg(), "paths": {"ai_backend": str(tmp_path), "dataset": str(tmp_path)}}
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_env", lambda: [])
    monkeypatch.setattr(R, "load", lambda: cfg)
    monkeypatch.setattr(R.json, "loads", lambda *a, **k: (_ for _ in ()).throw(Stop()))

    plan = tmp_path / "plan.json"
    plan.write_text("{}")
    with pytest.raises(Stop):
        R.main(["--plan", str(plan), "--mode", "batch"])
    assert cfg["run"]["model_mode"] == "batch"


def test_an_unknown_mode_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        R.main(["--plan", "x.json", "--mode", "async"])


def test_an_unknown_batch_stage_is_refused(monkeypatch, tmp_path):
    """Better a usage error than a run that silently batches nothing."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "load_env", lambda: [])
    monkeypatch.setattr(R, "load", lambda: {**_cfg(),
                                            "paths": {"ai_backend": str(tmp_path)}})
    assert R.main(["--plan", "x.json", "--mode", "batch",
                   "--batch-stages", "extract,refine"]) == 2


# --------------------------------------------------------------------- resume, second order
def test_resume_still_submits_what_the_stored_job_does_not_cover(tmp_path):
    """Reusing a --run-id after widening the plan must not silently collect the old job and
    drop the documents that were added."""
    p = FakeProvider({"a": ("{}", None), "b": ("{}", None)})
    d = _driver(tmp_path, p)
    list(d.run({"a": object()}, {}, "disp"))
    assert [keys for _, keys in p.submitted] == [["a"]]

    got = [r["key"] for r in d.run({"a": object(), "b": object()}, {}, "disp", resume=True)]
    assert [keys for _, keys in p.submitted] == [["a"], ["b"]], "the new request never went out"
    assert sorted(got) == ["a", "b"]


def test_collected_job_state_is_dropped_even_when_nothing_was_cacheable(tmp_path, monkeypatch):
    """The state file means 'in flight, unread'. Keeping it because every document happened
    to fail would make the next attempt replay the same dead job forever."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")
    runner._svc = FakeSvc(parts=("totals",), fail_sync=True)
    img = _png(tmp_path / "d1.png")

    st = _prepass(runner, FakeProvider({f"d1{B.SEP}totals": ("{", None)})).extract(
        ["d1"], {"d1": str(img)})
    assert st.batched == 0
    assert not (runner.out / "batch" / "extract.json").exists()


def test_an_interrupted_collection_keeps_the_state_to_resume_from(tmp_path):
    """The opposite case: the job did not finish, so the file has to survive."""
    class Stuck(FakeProvider):
        def retrieve_batch(self, name):
            job = super().retrieve_batch(name)
            job.state = "JOB_STATE_RUNNING"
            return job

    d = _driver(tmp_path, Stuck({"a": ("{}", None)}), timeout=0.05)
    with pytest.raises(B.BatchAborted):
        list(d.run({"a": object()}, {}, "disp"))
    assert (tmp_path / "s.json").exists()


# ------------------------------------------------------------------------- end-to-end parity
def test_a_batched_document_scores_identically_to_a_synchronous_one(tmp_path, monkeypatch):
    """The claim the whole module rests on. Given the same model output, a document that came
    through the batch and one that came through an interactive call must produce the same
    arms -- because once the cache is filled they are the same code path.

    The synchronous side is modelled the way `_run_parts` actually behaves: each part's
    response is validated through its schema and the results are merged. That step is not
    incidental -- it fills declared-but-absent fields with None and applies model defaults, so
    a comparison against the unvalidated dict would be comparing two different pipelines.
    """
    from ai.extraction.new_schema import RECEIPT_SCHEMA_MAP

    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "_run_with_timeout", lambda fn, t, label: fn())

    img = _png(tmp_path / "d1.png")
    part_payloads = {
        "parties": {"parties": {"seller": {"name": "Acme"}}},
        "totals": {"totals": {"totalIncludingTax": {"originalValue": "42.50"}}},
    }

    # --- synchronous: _run_parts validates each part, then merges ---
    sync_merged = {}
    for part, payload in part_payloads.items():
        sync_merged.update(RECEIPT_SCHEMA_MAP[part](**payload).model_dump())

    sync = R.Runner(_cfg(), ["RAW", "RAW_POSTPROCESSED"], "sync")
    monkeypatch.setattr(R.Runner, "_extract",
                        lambda self, r, i, d: (sync_merged, {"input": 1}))
    sync_result = sync.run_document("d1", str(img))
    assert sync_result.ok, sync_result.error

    # --- batch: the same answers arrive through the pre-pass, then run_document ---
    monkeypatch.undo()
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    monkeypatch.setattr(R, "_run_with_timeout", lambda fn, t, label: fn())
    batched = R.Runner(_cfg(), ["RAW", "RAW_POSTPROCESSED"], "batch")
    batched._svc = FakeSvc(parts=tuple(part_payloads))
    answers = {f"d1{B.SEP}{p}": (json.dumps(v), None) for p, v in part_payloads.items()}
    st = _prepass(batched, FakeProvider(answers)).extract(["d1"], {"d1": str(img)})
    assert st.batched == 1
    batched._batch_filled = {"extract": set(st.filled)}
    batch_result = batched.run_document("d1", str(img))

    assert batch_result.ok, batch_result.error
    assert batch_result.arms["RAW"] == sync_result.arms["RAW"]
    assert batch_result.arms["RAW_POSTPROCESSED"] == sync_result.arms["RAW_POSTPROCESSED"]
    # Same numbers, different provenance -- and the run says which.
    assert batch_result.stage_status["extract"] == "batch"
    assert sync_result.stage_status["extract"] == "ok"


def test_the_judge_is_not_batched_when_no_arm_uses_it(tmp_path, monkeypatch):
    """A RAW-only run never judges. Submitting five sections per document for it would be
    paid for before anything noticed nobody reads them."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW"], "t")           # no FINAL
    runner._svc = FakeSvc(parts=("totals",))
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())

    pre = _run_prepass_with(monkeypatch, runner, FakeProvider({}), ["d1"],
                            {"d1": str(img)}, ["extract", "judge"])
    assert [s.stage for s in pre.stats] == ["extract"], "the judge stage was submitted anyway"


def test_the_judge_is_batched_when_final_is_requested(tmp_path, monkeypatch):
    """The other half of the guard: it must not silently disable a stage that IS needed."""
    monkeypatch.setattr(R, "_ROOT", tmp_path)
    runner = R.Runner(_cfg(), ["RAW", "FINAL"], "t")
    runner._judge = FakeJudge()
    img = _png(tmp_path / "d1.png")
    _seed_extraction(runner, "d1", img, _receipt())
    names, _ = _sections_present(runner, img)

    p = FakeProvider({f"d1{B.SEP}{n}": (_SECTION_OK, None) for n in names})
    pre = _run_prepass_with(monkeypatch, runner, p, ["d1"], {"d1": str(img)}, ["judge"])
    assert [s.stage for s in pre.stats] == ["judge"]
    assert pre.stats[0].batched == 1
