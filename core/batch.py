"""Gemini Batch API pre-pass for the two paid stages.

WHAT THIS IS
The runner's own loop is unchanged. Before `run_plan` starts, this module submits every
model call the plan will make as one (or a few) Gemini batch jobs, waits for them, and writes
each result into the SAME response cache `Runner._extract` and `Runner._verify` already read.
`run_plan` then walks the documents exactly as it always has and finds every model stage
already answered.

That indirection is the whole design. A batch job is a submit-then-poll job over the WHOLE
plan; `run_document` is a per-document pipeline with per-stage timeouts, stage dumps, arms and
a spend cap. Trying to make one drive the other would have meant rewriting the runner. Filling
the cache instead means arms, `--resume`, `runs/<id>/stages/`, scoring and every test keep
working untouched, and a batch run and a sync run differ only in how the bytes were fetched.

    sync   extract:  4-5 interactive calls per document, `concurrency` documents at a time
           judge:    5 interactive calls per document
    batch  extract:  (4-5 x N) requests in one job        <- 50% cost, one wait for everything
           judge:    (<=5 x N) requests in one job

WHY THE CACHE KEY IS UNCHANGED
Model, prompt version, schema and prepared image bytes are identical on both paths -- the
batch request is built by the product's own `generate_batch_requests`, off the same
`prepare_extraction_image` output the sync path sends. A batch-produced extraction is the same
measurement as a sync one, so it is stored under the same key: an existing cache stays valid,
and switching modes never re-pays. Provenance is not lost -- the cached metadata carries
`batch: {...}`, and the manifest records the mode.

WHAT BATCH IS NOT
Not the path production runs. Production extracts interactively, per document, and the
harness's job is to measure production. Two behavioural differences are real and are recorded
rather than papered over:

  * NO STREAMING. `_stream_part` / `_stream_section` exist to publish think tokens to Redis.
    With job_id=None the harness already disabled that, so nothing is lost here -- but the
    batch response is a single blob, not a stream.
  * RETRIES DIFFER. Sync extraction retries a part up to 3 times inside `_run_parts` on
    truncated JSON or schema failure. A batch request gets one shot. A part that comes back
    broken is retried HERE, synchronously and at full price, through the product's own
    `extract_from_buffer` -- so it lands back on the retried path rather than being scored as
    a null. A document that still cannot be completed is simply not cached, and phase 3 runs
    it synchronously like any other cache miss.

The judge takes the coarser fallback: a document with ANY failed section is dropped from the
batch entirely and re-judged synchronously in phase 3. A JudgeReport has to be internally
consistent -- aggregate confidence is the MINIMUM over sections -- and splicing a
sync-retried section into a batch report is a way to get a report that no single code path
would ever have produced.

COST
Gemini bills batch at 50% of interactive. `compute_cost` has no batch flag, so the discount is
applied here, recorded as `batch.discount` next to the undiscounted figure, and a run that
mixes modes (sync retries, sync fallback) reports the true mixture rather than a flat halving.
"""
from __future__ import annotations

import io
import json
import pathlib
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

# Gemini's published batch discount. Not a tunable: it is what the bill says.
BATCH_DISCOUNT = 0.5

# Separator inside a batch key: "<doc_id>|||<part-or-section>". Document ids come from the
# dataset adapters (Template10_Instance103, X51005442388) and never contain it.
SEP = "|||"

# The cost dict compute_cost returns. Scaled together so the components still sum to the total.
_COST_FIELDS = ("input_cost", "output_cost", "thinking_cost", "total_cost")

# Inlined batch requests travel in the create call itself, so a job is bounded by request
# size, not just by count. An extraction part carries a ~1024px JPEG (~100-250KB), so a
# 5-part document is ~1MB and a few hundred documents would blow past any single request.
DEFAULT_MAX_REQUESTS_PER_JOB = 200
DEFAULT_MAX_MB_PER_JOB = 18.0
DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_JOB_TIMEOUT_S = 86_400.0          # Gemini's own SLO for a batch job

_TERMINAL = ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED",
             "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED")


class BatchAborted(RuntimeError):
    """A batch job failed, expired or was cancelled, or the projection exceeded the cap."""


# --------------------------------------------------------------------------------- helpers
def _state_name(job) -> str:
    """A batch job's state as a bare string, from either an enum or a string."""
    return str(getattr(job, "state", "") or "").split(".")[-1]


def _scale_cost(cost: Optional[Dict[str, Any]], factor: float) -> Dict[str, Any]:
    """Apply the batch discount to a compute_cost dict, keeping the undiscounted total.

    Every component is scaled, not just the total, so a reader summing the parts gets the
    same number the total says. `list_price_total_cost` is kept because a benchmark that
    reports what the product WOULD have paid interactively is more useful than one that
    silently reports the discounted figure.
    """
    if not isinstance(cost, dict):
        return {}
    out = dict(cost)
    out["list_price_total_cost"] = cost.get("total_cost")
    for f in _COST_FIELDS:
        if isinstance(cost.get(f), (int, float)):
            out[f] = round(cost[f] * factor, 8)
    out["batch_discount"] = factor
    return out


def _merge_tokens(into: Dict[str, int], tu: Dict[str, Any]) -> None:
    """Accumulate one call's usage, normalised to the sync path's shape.

    `get_batch_results` reports `output` as candidates-only and `thinking` separately, which
    is what `_stream_part` and `_stream_section` also produce -- so total is recomputed here
    rather than taken from the API's `total_token_count`, which counts them differently and
    would make a batch document's tokens incomparable with a sync one's.
    """
    for k in ("input", "output", "thinking"):
        into[k] = into.get(k, 0) + int(tu.get(k) or 0)
    into["total"] = into.get("input", 0) + into.get("output", 0) + into.get("thinking", 0)


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Parse one structured-output response. Raises ValueError on anything unusable.

    Gemini occasionally wraps a single object in a list when a schema's root is ambiguous;
    `_call_gemini` tolerates that on the sync path, so this does too.
    """
    parsed = json.loads((text or "").strip())
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# ------------------------------------------------------------------------------ job driver
class _JobDriver:
    """Submit a keyed request map as one or more batch jobs, then yield every result.

    Chunked by BOTH count and estimated payload size: inlined requests ride in the create
    call, and an extraction part carries a full JPEG.

    The job name is written to disk BEFORE the first poll, so a run killed during the wait
    can be resumed against the job it already paid for instead of submitting a second one.
    That file is the reason `--resume` is meaningful here at all: the money is spent at
    submit time, not at collect time.
    """

    def __init__(self, provider, state_path: pathlib.Path, stage: str, *,
                 poll_interval: float, timeout: float,
                 max_requests: int, max_bytes: float,
                 log: Callable[[str], None] = print):
        self.provider = provider
        self.state_path = state_path
        self.stage = stage
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.log = log

    # ---------------------------------------------------------------- state
    def _read_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.log(f"  !! {self.state_path.name} is not readable JSON; ignoring it")
        return {}

    def _write_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # ---------------------------------------------------------------- chunking
    def _chunks(self, requests_map: Dict[str, Any],
                sizes: Dict[str, int]) -> List[List[str]]:
        out: List[List[str]] = []
        cur: List[str] = []
        cur_bytes = 0
        cap = self.max_bytes * 1024 * 1024
        for key in requests_map:
            sz = sizes.get(key, 0)
            if cur and (len(cur) >= self.max_requests or cur_bytes + sz > cap):
                out.append(cur)
                cur, cur_bytes = [], 0
            cur.append(key)
            cur_bytes += sz
        if cur:
            out.append(cur)
        return out

    # ---------------------------------------------------------------- polling
    def _await(self, job_name: str) -> Any:
        deadline = time.monotonic() + self.timeout
        job = self.provider.retrieve_batch(job_name)
        last = None
        while _state_name(job) not in _TERMINAL:
            if time.monotonic() > deadline:
                raise BatchAborted(
                    f"{self.stage} job {job_name} did not reach a terminal state within "
                    f"{self.timeout:.0f}s. It is still running -- re-run with --resume to "
                    f"pick it up rather than submitting (and paying for) it again.")
            state = _state_name(job)
            if state != last:
                self.log(f"  {self.stage}: {job_name} {state}")
                last = state
            time.sleep(self.poll_interval)
            job = self.provider.retrieve_batch(job_name)
        return job

    # ---------------------------------------------------------------- run
    def run(self, requests_map: Dict[str, Any], sizes: Dict[str, int],
            display_name: str, resume: bool = True) -> Iterator[Dict[str, Any]]:
        """Submit (or resume) and yield {key, error, response_text, token_usage} per request."""
        state = self._read_state() if resume else {}
        chunks_state: List[Dict[str, Any]] = list(state.get("chunks") or [])

        if chunks_state:
            self.log(f"  {self.stage}: resuming {len(chunks_state)} already-submitted "
                     f"job(s) — those requests are paid for and are not sent again")

        # Whatever the stored jobs do not already cover still has to go out. Without this,
        # reusing a --run-id after widening the plan would silently collect the old job and
        # never submit the documents that were added.
        covered = {k for c in chunks_state for k in c["keys"]}
        outstanding = [k for k in requests_map if k not in covered]
        if outstanding:
            planned = self._chunks({k: requests_map[k] for k in outstanding}, sizes)
            total_mb = sum(sizes.get(k, 0) for k in outstanding) / 1024 / 1024
            self.log(f"  {self.stage}: {len(outstanding)} requests, ~{total_mb:.1f}MB, "
                     f"{len(planned)} job(s)")
            for i, keys in enumerate(planned, 1):
                sub = {k: requests_map[k] for k in keys}
                job = self.provider.create_batch(
                    sub, display_name=f"{display_name}-{i}of{len(planned)}")
                chunks_state.append({
                    "job": job.name,
                    "keys": keys,
                    "submitted_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                })
                # Persist before polling: from here on the tokens are committed, and a run
                # that dies during the wait must be able to find this job again.
                self._write_state({"stage": self.stage, "chunks": chunks_state})
                self.log(f"  {self.stage}: submitted {job.name} ({len(keys)} requests)")

        for chunk in chunks_state:
            job = self._await(chunk["job"])
            state_str = _state_name(job)
            if state_str not in ("JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"):
                err = getattr(getattr(job, "error", None), "message", None) or "no details"
                raise BatchAborted(
                    f"{self.stage} job {chunk['job']} ended {state_str}: {err}")
            if state_str == "JOB_STATE_PARTIALLY_SUCCEEDED":
                self.log(f"  {self.stage}: {chunk['job']} partially succeeded — the missing "
                         f"requests fall through to the synchronous path")
            # create_batch stamps the submission order onto the job object it returns; a job
            # fetched back by name has lost it, and get_batch_results would fall back to
            # "request_N" keys. Restore it from the state file.
            job._docuxray_keys = chunk["keys"]
            yield from self.provider.get_batch_results(job)

    def clear(self) -> None:
        """Drop the job state once every result has been COLLECTED.

        Collected, not cached. The file means "a job is in flight and its answers have not
        been read yet"; keeping it because nothing happened to be cacheable would make the
        next attempt replay the same failed job forever instead of submitting a new one.
        """
        self.state_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------- reporting
class BatchStats:
    """What the pre-pass did, for the console line and the manifest."""

    def __init__(self, stage: str):
        self.stage = stage
        self.requested = 0            # documents that needed this stage
        self.batched = 0              # documents fully served by the batch
        self.filled: List[str] = []   # the doc_ids behind `batched`, for the runner
        self.sync_retried = 0         # parts/sections re-run synchronously at full price
        self.fell_through = 0         # documents left for the synchronous path
        self.cost_usd = 0.0
        self.tokens: Dict[str, int] = {}
        self.jobs: List[str] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "documents_requested": self.requested,
            "documents_from_batch": self.batched,
            "sync_retries": self.sync_retried,
            "documents_fell_through_to_sync": self.fell_through,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "jobs": self.jobs,
        }

    def line(self) -> str:
        return (f"  {self.stage}: {self.batched}/{self.requested} from batch, "
                f"{self.sync_retried} sync retr{'y' if self.sync_retried == 1 else 'ies'}, "
                f"{self.fell_through} fell through  ${self.cost_usd:.4f}")


# --------------------------------------------------------------------------------- pre-pass
class BatchPrepass:
    """Fills a Runner's response cache from the Gemini Batch API before the run starts."""

    def __init__(self, runner, cfg: Dict[str, Any], *,
                 poll_interval: Optional[float] = None,
                 job_timeout: Optional[float] = None,
                 log: Callable[[str], None] = print):
        if runner.cache_dir is None:
            raise ValueError(
                "batch mode fills the response cache and the run then reads it back, so "
                "cache.enabled must be true in config.yaml. With the cache off there is "
                "nowhere to put a batch result and every document would be paid for twice.")
        self.r = runner
        self.cfg = cfg
        b = dict((cfg.get("run") or {}).get("batch") or {})
        self.poll_interval = float(poll_interval or b.get("poll_interval_s",
                                                          DEFAULT_POLL_INTERVAL_S))
        self.job_timeout = float(job_timeout or b.get("job_timeout_s", DEFAULT_JOB_TIMEOUT_S))
        self.max_requests = int(b.get("max_requests_per_job", DEFAULT_MAX_REQUESTS_PER_JOB))
        self.max_mb = float(b.get("max_mb_per_job", DEFAULT_MAX_MB_PER_JOB))
        self.log = log
        self.dir = runner.out / "batch"
        self.stats: List[BatchStats] = []
        self._provider = None

    @property
    def provider(self):
        if self._provider is None:
            from ai.llm.gemini_provider import GeminiProvider
            self._provider = GeminiProvider()
        return self._provider

    def _driver(self, stage: str) -> _JobDriver:
        return _JobDriver(
            self.provider, self.dir / f"{stage}.json", stage,
            poll_interval=self.poll_interval, timeout=self.job_timeout,
            max_requests=self.max_requests, max_bytes=self.max_mb, log=self.log)

    # ------------------------------------------------------------------ spend guard
    def _guard(self, stage: str, n_docs: int, calls_per_doc: int) -> None:
        """Refuse to submit a job whose projected cost would blow the cap.

        `run_plan`'s cap check runs per document, AFTER each one is paid for -- which is
        enough when calls trickle out one document at a time and useless here, where one
        `create_batch` commits the whole plan at once. The projection uses this cache's own
        measured cost per document, so it is an estimate only once: from the second run of a
        dataset onwards it is the real number.
        """
        per_doc = self._measured_cost_per_doc(stage)
        if per_doc is None:
            self.log(f"  {stage}: no cached {stage} cost to project from — submitting "
                     f"{n_docs} documents ({n_docs * calls_per_doc} requests) unpriced. "
                     f"The ${self.r.cap:.2f} cap cannot be enforced before the job is paid "
                     f"for; run a small --limit first if that matters.")
            return
        projected = per_doc * n_docs * BATCH_DISCOUNT
        self.log(f"  {stage}: projected ${projected:.2f} for {n_docs} documents "
                 f"(${per_doc:.5f}/doc measured, less the {BATCH_DISCOUNT:.0%} batch "
                 f"discount)")
        if self.r.spent + projected > self.r.cap:
            raise BatchAborted(
                f"submitting the {stage} batch would project ${self.r.spent + projected:.2f} "
                f"against a ${self.r.cap:.2f} cap. Nothing was submitted. Raise "
                f"run.spend_cap_usd or shrink the plan with --limit.")

    def _measured_cost_per_doc(self, stage: str) -> Optional[float]:
        """Mean cost per document of this stage's existing cache entries, if any."""
        costs: List[float] = []
        for p in self.r.cache_dir.glob(f"*.{stage}.json" if stage == "extract"
                                       else f"*.{stage}.*.json"):
            try:
                blob = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            meta = blob.get("metadata", blob)
            cost = meta.get("cost")
            if isinstance(cost, dict):
                # Project at LIST price: the discount is applied to the projection itself.
                total = cost.get("list_price_total_cost")
                if total is None:
                    total = cost.get("total_cost")
                if isinstance(total, (int, float)):
                    costs.append(float(total))
            if len(costs) >= 200:
                break
        return sum(costs) / len(costs) if costs else None

    # =================================================================== extraction
    def extract(self, doc_ids: List[str], images: Dict[str, str],
                resume: bool = True) -> BatchStats:
        """Fill the extract cache for every document that does not already have it."""
        from ai.pipeline_core import IMAGE_PREP_VERSION, prepare_extraction_image

        st = BatchStats("extract")
        todo: List[str] = []
        keys: Dict[str, str] = {}
        for d in doc_ids:
            image = pathlib.Path(images[d])
            k = self.r._cache_key(image, "extract")
            keys[d] = k
            if self.r._cache_get(k) is None:
                todo.append(d)
        st.requested = len(todo)
        if not todo:
            self.log("  extract: every document is already cached — nothing to submit")
            self.stats.append(st)
            return st

        self.log(f"  extract: preparing {len(todo)} document(s)")
        prepared: Dict[str, Tuple[bytes, str]] = {}
        requests_map: Dict[str, Any] = {}
        sizes: Dict[str, int] = {}
        parts_of: Dict[str, List[str]] = {}
        skipped: List[str] = []
        for d in todo:
            image = pathlib.Path(images[d])
            try:
                # The SAME preparation the sync path performs. The extractor is fed the
                # quality stage's clean image, never the file on disk, and that choice is
                # already baked into the cache key -- so a batch request built off anything
                # else would be cached under a key describing bytes it never saw.
                img_bytes, mime = prepare_extraction_image(
                    image.read_bytes(), apply_quality_transform=self.r.quality_transform)
                prepared[d] = (img_bytes, mime)
                pil = _open_image(img_bytes)
                for part, req in self.r.svc.generate_batch_requests(pil, part="all"):
                    key = f"{d}{SEP}{part}"
                    requests_map[key] = req
                    # The JPEG dominates a request; the prompt is a few KB of text.
                    sizes[key] = len(img_bytes) + 8192
                    parts_of.setdefault(d, []).append(part)
            except Exception as exc:                                     # noqa: BLE001
                self.log(f"  extract: {d} could not be prepared ({type(exc).__name__}: "
                         f"{exc}) — leaving it to the synchronous path")
                skipped.append(d)
        for d in skipped:
            todo.remove(d)
        st.fell_through += len(skipped)
        if not requests_map:
            self.stats.append(st)
            return st

        self._guard("extract", len(todo), max((len(v) for v in parts_of.values()), default=1))

        driver = self._driver("extract")
        t0 = time.time()
        collected: Dict[str, Dict[str, Any]] = {d: {} for d in todo}
        failed: Dict[str, List[str]] = {}
        usage: Dict[str, Dict[str, int]] = {d: {} for d in todo}
        per_part: Dict[str, List[Dict[str, Any]]] = {d: [] for d in todo}

        for res in driver.run(requests_map, sizes, "docuxray-benchmark-extract", resume):
            key = res.get("key") or ""
            if SEP not in key:
                self.log(f"  extract: unroutable batch key {key!r} — ignored")
                continue
            doc_id, part = key.split(SEP, 1)
            if doc_id not in collected:
                continue
            tu = res.get("token_usage") or {}
            _merge_tokens(usage[doc_id], tu)
            per_part[doc_id].append({
                "part": part,
                "prompt_tokens": tu.get("input") or None,
                "output_tokens": tu.get("output") or None,
                "thinking_tokens": tu.get("thinking") or None,
                "total_tokens": ((tu.get("input") or 0) + (tu.get("output") or 0)
                                 + (tu.get("thinking") or 0)) or None,
                "attempts": 1,
                "source": "batch",
            })
            if res.get("error"):
                self.log(f"  extract: {doc_id}/{part} — {res['error']}")
                per_part[doc_id][-1]["failed"] = True
                failed.setdefault(doc_id, []).append(part)
                continue
            try:
                parsed = _parse_json_object(res.get("response_text") or "")
                schema = self.r.svc._schema_map.get(part, self.r.svc._root_schema)
                # Validate exactly as `_validate_and_fill` does, but WITHOUT its
                # fill-with-None fallback: on the sync path an invalid part is retried
                # first, so accepting a filled-with-None part here would score a null the
                # product would have re-asked for.
                collected[doc_id][part] = schema(**parsed).model_dump()
            except Exception as exc:                                     # noqa: BLE001
                self.log(f"  extract: {doc_id}/{part} — {type(exc).__name__}: "
                         f"{str(exc)[:160]}")
                per_part[doc_id][-1]["failed"] = True
                failed.setdefault(doc_id, []).append(part)

        st.jobs = [c["job"] for c in (driver._read_state().get("chunks") or [])]

        # ---- synchronous retry, at full price, through the product's retried path ----
        for doc_id, parts in sorted(failed.items()):
            for part in parts:
                img_bytes, mime = prepared[doc_id]
                try:
                    res = self.r.svc.extract_from_buffer(
                        img_bytes, mime, part=part, document_id=doc_id, job_id=None)
                    collected[doc_id][part] = res.data
                    m = res.metadata or {}
                    _merge_tokens(usage[doc_id], {
                        "input": m.get("prompt_tokens") or 0,
                        "output": m.get("output_tokens") or 0,
                        "thinking": m.get("thinking_tokens") or 0,
                    })
                    per_part[doc_id].append({
                        "part": part,
                        "prompt_tokens": m.get("prompt_tokens"),
                        "output_tokens": m.get("output_tokens"),
                        "thinking_tokens": m.get("thinking_tokens"),
                        "total_tokens": m.get("total_tokens"),
                        "attempts": m.get("attempts") or 1,
                        "source": "sync_retry",
                        # Charged at list price, so it must not be swept into the discount.
                        "cost": (m.get("cost") or {}).get("total_cost"),
                    })
                    st.sync_retried += 1
                    self.log(f"  extract: {doc_id}/{part} recovered synchronously")
                except Exception as exc:                                 # noqa: BLE001
                    self.log(f"  extract: {doc_id}/{part} sync retry failed "
                             f"({type(exc).__name__}: {exc})")

        # ---- write one cache entry per complete document ----
        for doc_id in todo:
            want = parts_of.get(doc_id, [])
            have = collected[doc_id]
            if not want or any(p not in have for p in want):
                missing = [p for p in want if p not in have]
                self.log(f"  extract: {doc_id} incomplete (missing {missing}) — NOT cached, "
                         f"the run will extract it synchronously")
                st.fell_through += 1
                continue

            merged: Dict[str, Any] = {}
            for p in want:
                merged.update(have[p])

            tok = usage[doc_id]
            # Two different questions, and conflating them mis-states both the provenance and
            # the bill. `billed_by_batch` is every request the job ran -- a part whose answer
            # came back unparseable still generated tokens and still costs batch price.
            # `batch_parts` is the narrower set whose DATA is in this document.
            billed_by_batch = [x for x in per_part[doc_id] if x.get("source") == "batch"]
            batch_parts = [x for x in billed_by_batch if not x.get("failed")]
            sync_parts = [x for x in per_part[doc_id] if x.get("source") == "sync_retry"]
            # Only the calls the batch made get the discount. A document with a sync retry in
            # it costs the mixture, and says so.
            batch_tok: Dict[str, int] = {}
            for x in billed_by_batch:
                _merge_tokens(batch_tok, {"input": x.get("prompt_tokens") or 0,
                                          "output": x.get("output_tokens") or 0,
                                          "thinking": x.get("thinking_tokens") or 0})
            cost = self._blended_cost(batch_tok, sync_parts, self.cfg["models"]["extraction"])

            meta = {
                "documentType": self.r.doc_type,
                "model": self.cfg["models"]["extraction"],
                "parts_run": want,
                "prompt_tokens": tok.get("input") or None,
                "output_tokens": tok.get("output") or None,
                "thinking_tokens": tok.get("thinking") or None,
                "total_tokens": tok.get("total") or None,
                "attempts": sum(x.get("attempts") or 1 for x in per_part[doc_id]) or None,
                # A batch job's wall clock belongs to the JOB, not to one document; the
                # per-document figure the sync path reports has no batch equivalent, so it
                # is reported as the job wait rather than invented.
                "latency_ms": None,
                "cost": cost,
                "fallback_parts": [],
                "per_part": per_part[doc_id],
                "batch": {
                    "mode": "gemini_batch_api",
                    "discount": BATCH_DISCOUNT,
                    "jobs": st.jobs,
                    "parts_from_batch": [x["part"] for x in batch_parts],
                    "parts_from_sync_retry": [x["part"] for x in sync_parts],
                    "job_wait_ms": round((time.time() - t0) * 1000, 1),
                },
                "image_prep": {
                    "version": IMAGE_PREP_VERSION,
                    "quality_transform": self.r.quality_transform,
                    "mime": prepared[doc_id][1],
                    "source_bytes": len(prepared[doc_id][0]),
                },
            }
            meta["wall_ms"] = meta["batch"]["job_wait_ms"]
            self.r._cache_put(keys[doc_id], {"data": merged, "metadata": meta})
            st.batched += 1
            st.filled.append(doc_id)
            st.cost_usd += float((cost or {}).get("total_cost") or 0.0)
            _merge_tokens(st.tokens, tok)

        driver.clear()
        self.stats.append(st)
        return st

    def _blended_cost(self, batch_tokens: Dict[str, int],
                      sync_entries: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
        """Discounted cost for the batched calls plus list price for any sync retries."""
        from shared.pricing import compute_cost

        cost = _scale_cost(compute_cost(
            model_name=model,
            input_tokens=batch_tokens.get("input", 0),
            output_tokens=batch_tokens.get("output", 0),
            thinking_tokens=batch_tokens.get("thinking", 0),
        ), BATCH_DISCOUNT)
        extra = sum(float(x.get("cost") or 0.0) for x in sync_entries)
        if extra:
            cost["sync_retry_cost"] = round(extra, 8)
            cost["total_cost"] = round((cost.get("total_cost") or 0.0) + extra, 8)
            cost["list_price_total_cost"] = round(
                (cost.get("list_price_total_cost") or 0.0) + extra, 8)
        return cost

    # ======================================================================== judge
    def judge(self, doc_ids: List[str], images: Dict[str, str],
              resume: bool = True) -> BatchStats:
        """Fill the judge cache. Requires the extract cache to be populated already.

        The judge's cache key includes a hash of the data it is asked to verify, so this can
        only run once extraction has landed: the key does not exist until RAW does. That
        ordering is not an implementation detail, it is the pipeline -- judge_worker is fed
        `extraction_postprocessing_result`, so the benchmark's judge input is a function of
        its extraction output.
        """
        from ai.judge.core.judge import _strip_judge_fields
        from ai.judge.core.judge_models import SectionJudgeResponse
        from ai.judge.core.judge_prompts import get_section_name, get_section_prompts
        from ai.llm.base import ModelRequest, ThinkingLevel
        from ai.pipeline_core import WRAPPER_KEYS

        st = BatchStats("judge")
        judge = self.r.judge
        wrapper = WRAPPER_KEYS.get(self.r.doc_type)
        section_prompts = get_section_prompts(self.r.doc_type)

        jkeys: Dict[str, str] = {}
        sections_of: Dict[str, List[str]] = {}
        requests_map: Dict[str, Any] = {}
        sizes: Dict[str, int] = {}
        todo: List[str] = []

        for d in doc_ids:
            image = pathlib.Path(images[d])
            hit = self.r._cache_get(self.r._cache_key(image, "extract"))
            if hit is None:
                # Extraction fell through to the synchronous path, so RAW does not exist yet
                # and neither does this document's judge key. Phase 3 will do both.
                st.fell_through += 1
                continue
            try:
                raw_pp = self.r._extraction_postprocessing(hit["data"])
            except Exception as exc:                                     # noqa: BLE001
                self.log(f"  judge: {d} extraction_postprocessing failed "
                         f"({type(exc).__name__}: {exc}) — left to the synchronous path")
                st.fell_through += 1
                continue
            unwrapped = raw_pp.get(wrapper, raw_pp) if wrapper else raw_pp
            jkey = self.r._cache_key(image, "judge", unwrapped)
            jkeys[d] = jkey
            if self.r._cache_get(jkey) is not None:
                continue
            todo.append(d)

            # The SOURCE image, not prepare_extraction_image's output. `Runner._verify`
            # calls verify_sectional with `Image.open(image)` -- the file on disk -- so the
            # judge sees different pixels from the extractor. Sending the prepared JPEG here
            # would make a batch verdict incomparable with a sync one, and the judge's cache
            # key does not cover the difference.
            img_bytes = image.read_bytes()
            pil = _open_image(img_bytes)
            pil.load()
            for i, sp in enumerate(section_prompts):
                name = get_section_name(sp)
                # Section selection, verbatim from verify_sectional: a section whose data is
                # absent is SKIPPED, not sent. Sending it anyway would ask the judge to
                # verify null and manufacture issues production never sees.
                if sp.extra_keys:
                    sd: Any = {name: unwrapped.get(name)}
                    for k in sp.extra_keys:
                        sd[k] = unwrapped.get(k)
                    if all(v is None for v in sd.values()):
                        sd = None
                else:
                    sd = unwrapped.get(name)
                if sd is None:
                    continue
                prompt = judge._build_section_verification_prompt(
                    sp.prompt_text, _strip_judge_fields(sd))
                key = f"{d}{SEP}{name}"
                requests_map[key] = ModelRequest(
                    model=judge.model_name,
                    contents=[pil, prompt],
                    response_schema=SectionJudgeResponse,
                    thinking_level=ThinkingLevel.MEDIUM,
                )
                sizes[key] = len(img_bytes) + len(prompt.encode("utf-8")) + 4096
                sections_of.setdefault(d, []).append(name)

        st.requested = len(todo)
        if not requests_map:
            if todo:
                # verify_sectional would produce an empty report here without calling the
                # model, so the synchronous path handles it for free.
                st.fell_through += len(todo)
                self.log("  judge: no section had data to verify — nothing to submit")
            else:
                self.log("  judge: every document is already cached — nothing to submit")
            self.stats.append(st)
            return st

        self._guard("judge", len(todo),
                    max((len(v) for v in sections_of.values()), default=1))

        driver = self._driver("judge")
        t0 = time.time()
        results: Dict[str, Dict[str, Any]] = {d: {} for d in todo}
        broken: set = set()

        for res in driver.run(requests_map, sizes, "docuxray-benchmark-judge", resume):
            key = res.get("key") or ""
            if SEP not in key:
                self.log(f"  judge: unroutable batch key {key!r} — ignored")
                continue
            doc_id, name = key.split(SEP, 1)
            if doc_id not in results:
                continue
            if res.get("error"):
                self.log(f"  judge: {doc_id}/{name} — {res['error']}")
                broken.add(doc_id)
                continue
            try:
                parsed = _parse_json_object(res.get("response_text") or "")
                typed = SectionJudgeResponse(**parsed)
            except Exception as exc:                                     # noqa: BLE001
                self.log(f"  judge: {doc_id}/{name} — {type(exc).__name__}: "
                         f"{str(exc)[:160]}")
                broken.add(doc_id)
                continue
            results[doc_id][name] = (typed, res.get("token_usage") or {})

        st.jobs = [c["job"] for c in (driver._read_state().get("chunks") or [])]

        for doc_id in todo:
            want = sections_of.get(doc_id, [])
            have = results[doc_id]
            if doc_id in broken or any(n not in have for n in want):
                # Whole-document fallback, deliberately. Aggregate confidence is the MINIMUM
                # over sections, so a report missing a section is not a slightly incomplete
                # report -- it is a different, more confident one. Re-judge it whole.
                self.log(f"  judge: {doc_id} has a failed section — NOT cached, the run "
                         f"will judge it synchronously")
                st.fell_through += 1
                continue

            report = self._build_report(doc_id, want, have,
                                        judge.model_name, (time.time() - t0) * 1000, st.jobs)
            self.r._cache_put(jkeys[doc_id], report)
            st.batched += 1
            st.filled.append(doc_id)
            st.cost_usd += float((report.get("cost") or {}).get("total_cost") or 0.0)
            _merge_tokens(st.tokens, report.get("token_usage") or {})

        driver.clear()
        self.stats.append(st)
        return st

    def _build_report(self, doc_id: str, sections: List[str],
                      have: Dict[str, Any], model: str,
                      wait_ms: float, jobs: List[str]) -> Dict[str, Any]:
        """Assemble the report dict `_verify` caches, from this document's section responses.

        Mirrors verify_sectional's aggregation exactly -- issues keyed by section, tokens
        summed, aggregate confidence the MINIMUM over sections (not the mean) -- because
        `run_refinement_pipeline` reads `issues` out of this and step7 reads `confidence`.
        """
        from ai.judge.core.judge import calculate_cost

        issues: Dict[str, List[Dict[str, Any]]] = {}
        tokens: Dict[str, int] = {}
        confidences: List[float] = []
        per_section: List[Dict[str, Any]] = []

        for i, name in enumerate(sections):
            typed, tu = have[name]
            section_issues = typed.to_section_issues(name)
            for k, lst in section_issues.items():
                issues[k] = [x.model_dump() for x in lst]
            confidences.append(typed.confidence)
            sec_tokens: Dict[str, int] = {}
            _merge_tokens(sec_tokens, tu)
            _merge_tokens(tokens, tu)
            per_section.append({
                "section": name,
                "confidence": typed.confidence,
                "token_usage": sec_tokens,
                "cost": _scale_cost(calculate_cost(sec_tokens, model_name=model),
                                    BATCH_DISCOUNT),
                "source": "batch",
            })

        tokens["attempts"] = len(sections)
        total_issues = sum(len(v) for v in issues.values())
        return {
            "judge_passed": total_issues == 0,
            "total_issues": total_issues,
            "confidence": min(confidences) if confidences else 1.0,
            "issues": issues,
            "token_usage": tokens,
            "cost": _scale_cost(calculate_cost(tokens, model_name=model), BATCH_DISCOUNT),
            "verification_model": model,
            "prompt_version": "judge_v1",
            "wall_ms": round(wait_ms, 1),
            "per_section": per_section,
            "batch": {"mode": "gemini_batch_api", "discount": BATCH_DISCOUNT, "jobs": jobs},
        }

    # ------------------------------------------------------------------ manifest
    def manifest_block(self) -> Dict[str, Any]:
        return {
            "mode": "batch",
            "discount": BATCH_DISCOUNT,
            "poll_interval_s": self.poll_interval,
            "job_timeout_s": self.job_timeout,
            "max_requests_per_job": self.max_requests,
            "max_mb_per_job": self.max_mb,
            "stages": [s.as_dict() for s in self.stats],
        }


# --------------------------------------------------------------------------------- shared
def _open_image(img_bytes: bytes):
    from PIL import Image
    return Image.open(io.BytesIO(img_bytes))


def run_prepass(runner, cfg: Dict[str, Any], doc_ids: List[str], images: Dict[str, str], *,
                stages: List[str], resume: bool = True,
                poll_interval: Optional[float] = None,
                job_timeout: Optional[float] = None,
                log: Callable[[str], None] = print) -> BatchPrepass:
    """Run the batch pre-pass for the requested stages, in pipeline order.

    Order is not a preference: the judge's cache key hashes the data it verifies, which does
    not exist until extraction has landed.
    """
    pre = BatchPrepass(runner, cfg, poll_interval=poll_interval,
                       job_timeout=job_timeout, log=log)
    pre.dir.mkdir(parents=True, exist_ok=True)
    stages = list(stages)
    # Only arm FINAL calls the judge. Batching it for a RAW-only run would submit -- and pay
    # for -- five sections per document that no arm will ever read. The runner refuses an
    # unknown arm loudly; this one has to be caught here, because the request is legal and
    # merely pointless, and the bill arrives before anything notices.
    if "judge" in stages and "FINAL" not in runner.arms:
        log(f"  judge: not batched — no requested arm uses it (arms "
            f"{','.join(runner.arms)}). Add FINAL, or drop 'judge' from --batch-stages.")
        stages.remove("judge")
    log(f"batch pre-pass: stages {','.join(stages) or '(none)'} over "
        f"{len(doc_ids)} document(s)")
    if "extract" in stages:
        log(pre.extract(doc_ids, images, resume).line())
    if "judge" in stages:
        log(pre.judge(doc_ids, images, resume).line())
    log("")
    return pre
