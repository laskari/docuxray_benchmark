"""Run the frozen DocuXray pipeline over a benchmark plan.

Arms mirror production's ACTUAL stage order, verified against the worker enqueue chain:

    extraction_worker ── run_extraction_postprocessing_pipeline ──> judge_queue
    judge_worker ──> refinement_queue
    refinement_worker ──> postprocessing_queue
    postprocessing_worker  (LAST, on refined_data, first_pass=True)

so:

    A  extract                                    raw model capability
    B  A + extraction_postprocessing              <-- WHAT THE JUDGE ACTUALLY SEES
    C  B + judge + refinement                     judge contribution
    D  C + postprocessing(first_pass=True)        the shipped output

The type postprocessor (invoice_postprocessor) runs AFTER refinement, not before it.
judge_worker.py:88 feeds the judge `extraction_postprocessing_result or extraction_result`,
never the postprocessor's output. An earlier version of this file ran postprocessing in arm B
and so fed the judge normalised numbers it never sees in production -- which manufactured a
"the judge reverts normalisation" finding that was an artifact of the harness, not a defect in
the product. Getting the stage order wrong does not produce a slightly-off number; it produces
a confident, wrong story.

B and D add NO model call and refinement adds none either, so one document is still
4 extraction calls + 5 judge sections, and all four arms come out of that single pass.

Nothing here mutates the product. job_id is always None, which is what keeps Redis and MongoDB
out of the path (every publish/persist site in extraction and the judge is guarded by it).

EVERY STAGE IS WRITTEN TO DISK THE MOMENT IT RETURNS, under

    runs/<id>/stages/<doc_id>/00_status.json                     progress, updated per stage
                              01_extract_raw.json                arm A, straight from Gemini
                              02_extraction_postprocessing.json  arm B, the judge's input
                              03_judge_report.json               the judge's verdict
                              04_refined.json                    arm C
                              05_postprocessed.json              arm D, the shipped output

so a document that dies in the judge still leaves its extraction behind, and a long run can be
watched by tailing 00_status.json instead of guessing. runs/<id>/raw/<doc_id>.json is unchanged
-- step6 and step7 read that and only that.
"""
from __future__ import annotations

import concurrent.futures as futures
import hashlib
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from config import load, load_env                      # noqa: E402
from core.canonical import read_jsonl                       # noqa: E402
import doctypes                                            # noqa: E402


class SpendCapExceeded(RuntimeError):
    pass


class StageTimeout(RuntimeError):
    """One pipeline stage did not come back inside its wall-clock budget."""


# Threads abandoned by _run_with_timeout. They are daemons, so they cannot hold the
# interpreter open, but a run that leaked any is not a clean run and says so.
_LEAKED: List[str] = []
_LEAKED_LOCK = threading.Lock()

STAGE_FILES = {
    "extract":                    "01_extract_raw.json",
    "extraction_postprocessing":  "02_extraction_postprocessing.json",
    "judge":                      "03_judge_report.json",
    "refine":                     "04_refined.json",
    "postprocess":                "05_postprocessed.json",
}


def _run_with_timeout(fn: Callable[[], Any], timeout: float, label: str) -> Any:
    """Run fn() on a daemon thread and stop waiting after `timeout` seconds.

    Nothing here can *cancel* a stalled Gemini call. ai/llm builds its client as
    `genai.Client(api_key=key)` with no http_options (gemini_provider.py:83), so there is no
    HTTP read timeout, and the judge's own fan-out calls `as_completed(futures)` with no
    timeout either (judge.py:705) -- one section that never returns hangs the whole document,
    permanently. Extraction at least has `max_retries = 3` with backoff; the judge has no
    retry path at all, so a stalled section is terminal.

    What this CAN do is stop waiting. The thread is a daemon on purpose: concurrent.futures
    registers an atexit hook that joins its worker threads, so an abandoned executor worker
    would keep the process alive after the run is over -- which is exactly what "stuck, no
    output, will not even Ctrl-C" looked like.
    """
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:                              # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_target, name=f"stage-{label}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        with _LEAKED_LOCK:
            _LEAKED.append(label)
        raise StageTimeout(f"{label} did not return within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


@dataclass
class DocResult:
    doc_id: str
    ok: bool = False
    cached: bool = False          # true only when EVERY model stage came from cache
    cached_stages: Dict[str, bool] = field(default_factory=dict)
    timed_out: bool = False
    error: Optional[str] = None
    error_stage: Optional[str] = None
    error_traceback: Optional[str] = None
    stage_status: Dict[str, str] = field(default_factory=dict)   # stage -> ok|cache|timeout|error
    arms: Dict[str, Any] = field(default_factory=dict)           # arm -> extracted data
    judge: Optional[Dict[str, Any]] = None
    tokens: Dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    attempts: int = 0
    latency_ms: Dict[str, float] = field(default_factory=dict)   # model-reported
    stage_ms: Dict[str, float] = field(default_factory=dict)     # harness wall clock


class Runner:
    # Defaults if config.yaml carries no run.stage_timeouts block. `judge` is deliberately
    # generous: a legitimate totals section has been observed at 136s, so anything under about
    # 300s would start failing healthy documents.
    DEFAULT_STAGE_TIMEOUTS = {"extract": 300.0, "judge": 420.0, "free": 60.0}

    def __init__(self, cfg: Dict[str, Any], arms: List[str], run_id: str,
                 stage_timeouts: Optional[Dict[str, float]] = None):
        self.cfg, self.arms, self.run_id = cfg, arms, run_id
        self.doc_type = cfg["run"]["doc_type"]
        self.cap = float(cfg["run"]["spend_cap_usd"])
        self.early_abort_after = int(cfg["run"].get("early_abort_after", 0))
        self.cache_dir = _ROOT / cfg["cache"]["dir"] if cfg["cache"]["enabled"] else None
        if self.cache_dir:
            self.cache_dir.mkdir(exist_ok=True)
        self.out = _ROOT / "runs" / run_id
        (self.out / "raw").mkdir(parents=True, exist_ok=True)
        self.stages_dir = self.out / "stages"
        self.stages_dir.mkdir(parents=True, exist_ok=True)

        t = dict(self.DEFAULT_STAGE_TIMEOUTS)
        t.update({k: float(v) for k, v in (cfg["run"].get("stage_timeouts") or {}).items()})
        t.update({k: float(v) for k, v in (stage_timeouts or {}).items() if v})
        self.stage_timeouts = t

        self.spent = 0.0
        self._svc = None
        self._judge = None

    # ---------------------------------------------------------------- lazy production handles
    @property
    def svc(self):
        if self._svc is None:
            from ai.extraction.gemini_extraction_service import GeminiExtractionService
            self._svc = GeminiExtractionService(doc_type=self.doc_type)
        return self._svc

    @property
    def judge(self):
        if self._judge is None:
            from ai.judge.core.judge import DocumentJudge
            self._judge = DocumentJudge()
        return self._judge

    # ---------------------------------------------------------------- cache
    def _cache_key(self, image: pathlib.Path, stage: str, payload: Any = None) -> str:
        """sha256(image) + model + prompt_version + stage, plus -- for the judge -- a hash of
        the data it is being asked to verify.

        The payload term is not optional. The judge's answer depends entirely on its input, so
        a key that ignores the input will happily serve a cached verdict computed from data the
        pipeline no longer produces. That is how a stage-order bug survives a re-run.
        """
        h = hashlib.sha256(image.read_bytes()).hexdigest()[:16]
        model = self.cfg["models"]["extraction" if stage == "extract" else "judge"]
        pv = self.cfg.get("prompt_version", "frozen")
        key = f"{h}.{model}.{pv}.{stage}"
        if payload is not None:
            blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            key += "." + hashlib.sha256(blob).hexdigest()[:12]
        return key

    def _cache_get(self, key: str):
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"{key}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _cache_put(self, key: str, value) -> None:
        if self.cache_dir:
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8")

    # ---------------------------------------------------------------- intermediate output
    def _doc_dir(self, doc_id: str) -> pathlib.Path:
        d = self.stages_dir / doc_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_stage(self, r: DocResult, stage: str, payload: Any, **meta) -> None:
        """Persist one stage the moment it returns, then refresh the status file.

        Written per stage rather than once per document so that a document lost in the judge
        still leaves its extraction on disk. Before this, a stalled judge section threw away
        four extraction calls that had already been paid for.
        """
        body = {
            "doc_id": r.doc_id,
            "stage": stage,
            "arm": {"extract": "A", "extraction_postprocessing": "B",
                    "judge": None, "refine": "C", "postprocess": "D"}.get(stage),
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cached": bool(r.cached_stages.get(stage, False)),
            "stage_ms": round(r.stage_ms.get(stage, 0.0), 1),
            **meta,
            "data": payload,
        }
        (self._doc_dir(r.doc_id) / STAGE_FILES[stage]).write_text(
            json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self._write_status(r)

    def _write_status(self, r: DocResult, done: bool = False) -> None:
        (self._doc_dir(r.doc_id) / "00_status.json").write_text(json.dumps({
            "doc_id": r.doc_id,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": self.run_id,
            "arms_requested": self.arms,
            "finished": done,
            "ok": r.ok,
            "timed_out": r.timed_out,
            "stage_status": r.stage_status,
            "stage_ms": {k: round(v, 1) for k, v in r.stage_ms.items()},
            "cached_stages": r.cached_stages,
            "files": {s: STAGE_FILES[s] for s in r.stage_status if s in STAGE_FILES},
            "tokens": r.tokens,
            "cost_usd": round(r.cost_usd, 6),
            "error_stage": r.error_stage,
            "error": r.error,
        }, indent=2, default=str), encoding="utf-8")

    # ---------------------------------------------------------------- stage driver
    def _stage(self, r: DocResult, name: str, fn: Callable[[], Any],
               budget: str | None = None) -> Any:
        """Run one stage under a hard wall-clock limit and record what happened.

        Every stage goes through here -- including the free ones -- so that stage_status is a
        complete account of the document and one pathological postprocessor cannot stall a
        1,000-document run.
        """
        timeout = self.stage_timeouts.get(budget or name, self.stage_timeouts["free"])
        t0 = time.time()
        try:
            value = _run_with_timeout(fn, timeout, f"{r.doc_id}:{name}")
        except StageTimeout as exc:
            r.stage_ms[name] = (time.time() - t0) * 1000
            r.stage_status[name] = "timeout"
            r.timed_out = True
            r.error_stage = name
            r.error = f"StageTimeout: {exc}"
            self._write_status(r, done=True)
            raise
        except Exception as exc:                                     # noqa: BLE001
            r.stage_ms[name] = (time.time() - t0) * 1000
            r.stage_status[name] = "error"
            r.error_stage = name
            r.error = f"{type(exc).__name__}: {exc}"
            self._write_status(r, done=True)
            raise
        r.stage_ms[name] = (time.time() - t0) * 1000
        r.stage_status[name] = "cache" if r.cached_stages.get(name) else "ok"
        return value

    # ---------------------------------------------------------------- stages
    def _extraction_postprocessing(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Arm B: field removal + leaf expansion + totals conversion. No model call.

        This is exactly what the judge is fed in production (judge_worker.py:88). It does NOT
        include the type postprocessor -- that runs last, in arm D.
        """
        from ai.extraction_postprocessing.core.service import run_extraction_postprocessing
        import copy
        return run_extraction_postprocessing(copy.deepcopy(raw), self.doc_type)

    def _postprocess(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Arm D: the type postprocessor, run on REFINED data, exactly as
        postprocessing_worker.py:121 does -- unwrap, process(first_pass=True), re-wrap."""
        from ai.postprocessing import get_postprocessor, get_wrapper_key
        import copy

        out = copy.deepcopy(data)
        processor = get_postprocessor(self.doc_type)
        wrapper = get_wrapper_key(self.doc_type)
        if wrapper and isinstance(out.get(wrapper), dict):
            out[wrapper] = processor.process(out[wrapper], job_id=None, first_pass=True)
        else:
            out = processor.process(out, job_id=None, first_pass=True)
        return out

    def _extract(self, r: DocResult, image: pathlib.Path, doc_id: str):
        key = self._cache_key(image, "extract")
        hit = self._cache_get(key)
        if hit:
            r.cached_stages["extract"] = True
            return hit["data"], hit["metadata"]
        r.cached_stages["extract"] = False
        t0 = time.time()
        res = self.svc.extract(str(image), part="all", document_id=doc_id, job_id=None)
        raw, meta = res.data, res.metadata
        meta["wall_ms"] = (time.time() - t0) * 1000
        self._cache_put(key, {"data": raw, "metadata": meta})
        return raw, meta

    def _verify(self, r: DocResult, image: pathlib.Path, doc_id: str, unwrapped: Dict[str, Any],
                jkey: str):
        hit = self._cache_get(jkey)
        if hit:
            r.cached_stages["judge"] = True
            return hit
        r.cached_stages["judge"] = False
        from PIL import Image
        t0 = time.time()
        # NOT run_judge_pipeline: its signature forces a job_id through to verify_sectional,
        # which re-enables the Mongo and Redis paths.
        rep = self.judge.verify_sectional(
            image=Image.open(image), document_type=self.doc_type,
            extracted_data=unwrapped, document_id=doc_id,
            job_id=None, page_index=None,
        )
        report = {
            "judge_passed": not rep.has_issues(),
            "total_issues": rep.summary.total_issues,
            "confidence": rep.summary.confidence,
            "issues": {s: [i.model_dump() for i in lst] for s, lst in rep.issues.items()},
            "token_usage": rep.token_usage,
            "cost": rep.cost,
            "verification_model": rep.verification_model,
            "prompt_version": rep.prompt_version,
            "wall_ms": (time.time() - t0) * 1000,
        }
        self._cache_put(jkey, report)
        return report

    def run_document(self, doc_id: str, image_path: str) -> DocResult:
        r = DocResult(doc_id=doc_id)
        image = pathlib.Path(image_path)
        try:
            if not image.exists():
                raise FileNotFoundError(image)
            self._write_status(r)

            # ---- arm A -------------------------------------------------------------
            raw, meta = self._stage(r, "extract", lambda: self._extract(r, image, doc_id))
            r.arms["A"] = raw
            r.latency_ms["extract"] = meta.get("latency_ms") or meta.get("wall_ms", 0)
            self._account(r, meta)
            self._write_stage(r, "extract", raw, metadata=meta,
                              model=self.cfg["models"]["extraction"],
                              image=str(image), latency_ms=r.latency_ms["extract"])

            # ---- arm B (free) ------------------------------------------------------
            if {"B", "C", "D"} & set(self.arms):
                r.arms["B"] = self._stage(r, "extraction_postprocessing",
                                          lambda: self._extraction_postprocessing(raw),
                                          budget="free")
                self._write_stage(r, "extraction_postprocessing", r.arms["B"],
                                  note="this, unwrapped, is exactly what the judge is fed "
                                       "(judge_worker.py:88)")

            # ---- arm C -------------------------------------------------------------
            if {"C", "D"} & set(self.arms):
                from ai.judge.pipeline import _EXTRACTION_WRAPPER_KEYS
                from ai.refinement.pipeline import run_refinement_pipeline

                base = r.arms["B"]
                wrapper = _EXTRACTION_WRAPPER_KEYS.get(self.doc_type)
                unwrapped = base.get(wrapper, base) if wrapper else base
                jkey = self._cache_key(image, "judge", unwrapped)

                report = self._stage(r, "judge",
                                     lambda: self._verify(r, image, doc_id, unwrapped, jkey))
                r.judge = report
                r.latency_ms["judge"] = report.get("wall_ms", 0)
                self._account(r, report)
                self._write_stage(r, "judge", report,
                                  model=self.cfg["models"]["judge"],
                                  judge_input_sha=jkey.rsplit(".", 1)[-1],
                                  total_issues=report.get("total_issues"),
                                  confidence=report.get("confidence"))

                r.arms["C"] = self._stage(
                    r, "refine",
                    lambda: run_refinement_pipeline(base, report, self.doc_type)["refined_data"],
                    budget="free")
                self._write_stage(r, "refine", r.arms["C"])

            # ---- arm D (free): the shipped output --------------------------------
            if "D" in self.arms and "C" in r.arms:
                r.arms["D"] = self._stage(r, "postprocess",
                                          lambda: self._postprocess(r.arms["C"]), budget="free")
                self._write_stage(r, "postprocess", r.arms["D"],
                                  note="the shipped output: postprocessing(first_pass=True) "
                                       "on refined data")

            r.ok = True
            r.cached = bool(r.cached_stages) and all(r.cached_stages.values())
        except StageTimeout:
            pass                     # _stage already recorded it and wrote the status file
        except Exception as exc:                                     # noqa: BLE001
            r.error = r.error or f"{type(exc).__name__}: {exc}"
            r.error_traceback = traceback.format_exc()[-2000:]
        self._write_status(r, done=True)
        return r

    # The two stages report usage in DIFFERENT shapes, which is how the first cost probe came
    # back $0.00 with tokens counted:
    #   extraction -> flat keys on metadata: prompt_tokens / output_tokens / thinking_tokens
    #   judge      -> nested metadata["token_usage"]: input / output / thinking
    # and the cost dict's total is keyed "total_cost", not "total_usd".
    _TOKEN_ALIASES = {
        "input":    ("input", "prompt_tokens", "input_tokens"),
        "output":   ("output", "output_tokens"),
        "thinking": ("thinking", "thinking_tokens"),
        "total":    ("total", "total_tokens"),
    }

    def _account(self, r: DocResult, meta: Dict[str, Any]) -> None:
        usage = meta.get("token_usage") if isinstance(meta.get("token_usage"), dict) else meta
        for canon, aliases in self._TOKEN_ALIASES.items():
            for a in aliases:
                if isinstance(usage.get(a), (int, float)):
                    r.tokens[canon] = r.tokens.get(canon, 0) + int(usage[a])
                    break
        r.attempts = r.attempts + int(usage.get("attempts") or meta.get("attempts") or 0)

        cost = meta.get("cost")
        if isinstance(cost, dict):
            cost = (cost.get("total_cost") if cost.get("total_cost") is not None
                    else cost.get("total_usd") if cost.get("total_usd") is not None
                    else cost.get("total"))
        r.cost_usd += float(cost or 0.0)

    # ---------------------------------------------------------------- plan
    def per_doc_budget(self) -> float:
        """Worst case for one document, from the stage budgets that now bound it."""
        t = self.stage_timeouts
        return t["extract"] + (t["judge"] if {"C", "D"} & set(self.arms) else 0.0) + 3 * t["free"]

    def run_plan(self, doc_ids: List[str], images: Dict[str, str],
                 doc_timeout: Optional[float] = None) -> List[DocResult]:
        """Run the plan. No stage can hang, so the global deadline is only a backstop.

        Each stage runs under its own wall-clock budget (see _run_with_timeout), so a stalled
        judge section now fails ONE document, keeps whatever that document had already
        produced, and the queue keeps moving. The deadline below exists only for a failure the
        stage budgets cannot see -- and the shutdown stays non-blocking, because a hung worker
        must never hold the process open.
        """
        results: List[DocResult] = []
        n = len(doc_ids)
        workers = int(self.cfg["run"].get("concurrency", 2))
        budget = doc_timeout or self.per_doc_budget()
        deadline = budget * math.ceil(max(n, 1) / max(workers, 1)) + budget
        print(f"run {self.run_id}: {n} documents, arms {','.join(self.arms)}, "
              f"concurrency {workers}, cap ${self.cap:.2f}\n"
              f"  stage budgets: extract {self.stage_timeouts['extract']:.0f}s · "
              f"judge {self.stage_timeouts['judge']:.0f}s · free "
              f"{self.stage_timeouts['free']:.0f}s  ->  {budget:.0f}s/doc, "
              f"{deadline / 60:.0f}min backstop\n"
              f"  stages -> {self.stages_dir}\n")

        pool = futures.ThreadPoolExecutor(max_workers=workers)
        pending = {pool.submit(self.run_document, d, images[d]): d for d in doc_ids}
        done = 0
        try:
            for fut in futures.as_completed(pending, timeout=deadline):
                r = fut.result(); results.append(r); done += 1
                self.spent += r.cost_usd
                (self.out / "raw" / f"{r.doc_id}.json").write_text(
                    json.dumps(asdict(r), ensure_ascii=False, default=str), encoding="utf-8")
                flag = ("cache" if r.cached else "ok   ") if r.ok else \
                       ("TMOUT" if r.timed_out else "FAIL ")
                stages = " ".join(f"{k}={v}" for k, v in sorted(r.stage_status.items()))
                print(f"  [{done:>3d}/{n}] {flag} {r.doc_id:24s} ${self.spent:7.3f}  "
                      f"{stages}" + (f"  {r.error}" if r.error else ""), flush=True)

                if self.spent > self.cap:
                    for f2 in pending:
                        f2.cancel()
                    raise SpendCapExceeded(
                        f"spend ${self.spent:.2f} exceeded the ${self.cap:.2f} cap after "
                        f"{done} documents; {n - done} not run. Partial manifest written.")
                if self.early_abort_after and done == self.early_abort_after:
                    projected = self.spent / done * n
                    print(f"        projection after {done}: ${projected:.2f} for {n} docs")
                    if projected > self.cap:
                        for f2 in pending:
                            f2.cancel()
                        raise SpendCapExceeded(
                            f"projected ${projected:.2f} exceeds the ${self.cap:.2f} cap; aborted "
                            f"after {done} documents rather than overrunning.")
        except futures.TimeoutError:
            stalled = [d for f, d in pending.items() if not f.done()]
            print(f"\n!! BACKSTOP DEADLINE hit after {deadline:.0f}s with {len(stalled)} "
                  f"document(s) still in flight:", flush=True)
            for d in stalled[:10]:
                print(f"     {d}")
            print("   Every stage that completed is on disk under stages/<doc_id>/.\n"
                  "   The per-stage budgets should have caught this first, so treat it as a\n"
                  "   harness bug rather than a slow model: lower `concurrency` in config.yaml\n"
                  "   and re-run the same command with --resume.", flush=True)
            for d in stalled:
                results.append(DocResult(doc_id=d, ok=False, timed_out=True,
                                         error=f"backstop deadline after {deadline:.0f}s"))
        finally:
            # wait=False: a hung worker must not hold the process open.
            pool.shutdown(wait=False, cancel_futures=True)

        # A timed-out document gets a raw/*.json too, so --resume knows to retry it and
        # step6/step7 (which skip ok=false) stay unaffected.
        for r in results:
            p = self.out / "raw" / f"{r.doc_id}.json"
            if not p.exists():
                p.write_text(json.dumps(asdict(r), ensure_ascii=False, default=str),
                             encoding="utf-8")
        return results

    # ---------------------------------------------------------------- manifest
    def manifest(self, results: List[DocResult], plan: Dict[str, Any], extra=None) -> Dict:
        ok = [r for r in results if r.ok]
        stage_fail: Dict[str, int] = {}
        for r in results:
            for stage, status in (r.stage_status or {}).items():
                if status in ("timeout", "error"):
                    stage_fail[f"{stage}:{status}"] = stage_fail.get(f"{stage}:{status}", 0) + 1
        m = {
            "run_id": self.run_id,
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "plan": plan.get("_path"),
            "n_planned": len(plan.get("doc_ids", [])),
            "n_run": len(results),
            "n_ok": len(ok),
            "n_failed": len(results) - len(ok),
            "n_cached": sum(1 for r in results if r.cached),
            "n_timed_out": sum(1 for r in results if getattr(r, "timed_out", False)),
            "stage_failures": stage_fail,
            "leaked_threads": sorted(_LEAKED),
            "arms": self.arms,
            "doc_type": self.doc_type,
            "models": self.cfg["models"],
            "stage_timeouts_s": self.stage_timeouts,
            "concurrency": int(self.cfg["run"].get("concurrency", 2)),
            "spend_cap_usd": self.cap,
            "total_cost_usd": round(self.spent, 4),
            "cost_per_doc_usd": round(self.spent / max(len(ok), 1), 5),
            "tokens": {k: sum(r.tokens.get(k, 0) for r in results)
                       for k in ("input", "output", "thinking", "total")},
            "model_call_attempts": sum(getattr(r, "attempts", 0) for r in results),
            "git": _git_shas(),
            "harness_version": "1.1",
            "field_map_version": _map_version(self.cfg.get("_map_path")),
            "dataset": self.cfg.get("_dataset_name"),
            "frozen_system": True,
        }
        if extra:
            m.update(extra)
        (self.out / "manifest.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
        return m


def _git_shas() -> Dict[str, str]:
    out = {}
    for repo in ("docuxray_ai_backend", "docuxary_backend", "docuxary_frontend"):
        p = _ROOT / ".." / repo
        try:
            sha = subprocess.run(["git", "-C", str(p), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=10)
            dirty = subprocess.run(["git", "-C", str(p), "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=10)
            out[repo] = (sha.stdout.strip() or "?") + ("+dirty" if dirty.stdout.strip() else "")
        except Exception:
            out[repo] = "?"
    return out


def _map_version(map_path=None) -> str:
    import yaml
    p = pathlib.Path(map_path) if map_path else (_ROOT / "mapping" / "fatura_invoice.map.yaml")
    if not p.is_absolute():
        p = _ROOT / p
    return yaml.safe_load(p.read_text(encoding="utf-8")).get("version", "?") if p.exists() else "?"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--arms", default="A,B,C,D",
                    help="A extract · B +extraction_postprocessing (judge input) · "
                         "C +judge+refine · D +postprocessing (shipped output)")
    ap.add_argument("--limit", type=int, default=0, help="first N documents only (cost probe)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--resume", action="store_true",
                    help="skip documents that already have a successful raw/*.json in this run")
    ap.add_argument("--doc-timeout", type=float, default=None,
                    help="backstop only; defaults to the sum of the stage budgets")
    ap.add_argument("--extract-timeout", type=float, default=None,
                    help="seconds for the 4-part extraction stage (default 300)")
    ap.add_argument("--judge-timeout", type=float, default=None,
                    help="seconds for the 5-section judge stage (default 420). A stalled "
                         "section is the one hang observed in practice.")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="override config.yaml; lower this first if a run stalls")
    a = ap.parse_args(argv)

    load_env()
    cfg = load()
    sys.path.insert(0, cfg["paths"]["ai_backend"])

    plan = json.loads(pathlib.Path(a.plan).read_text(encoding="utf-8"))
    plan["_path"] = a.plan
    doc_ids = plan["doc_ids"][: a.limit] if a.limit else plan["doc_ids"]

    spec = doctypes.get(cfg["run"]["doc_type"])
    gt = {r.doc_id: r for r in read_jsonl(str(_ROOT / "gt" / spec.name / "ground_truth.jsonl"))}
    not_in_gt = [d for d in doc_ids if d not in gt]
    if not_in_gt:
        print(f"!! {len(not_in_gt)} plan documents are not in the ground truth: {not_in_gt[:5]}")
        return 2

    # Ground-truth image paths are relative to the dataset root so the same gt/fatura.jsonl
    # works on any machine. Absolute paths from an older build are still honoured.
    dataset = pathlib.Path(cfg["paths"]["dataset"])
    images = {}
    for d in doc_ids:
        raw = pathlib.Path(gt[d].image_path)
        images[d] = str(raw if raw.is_absolute() else dataset / raw)

    absent = [d for d in doc_ids if not pathlib.Path(images[d]).exists()]
    if absent:
        # Fail once, before spending anything, rather than N identical errors.
        print(f"!! {len(absent)} of {len(doc_ids)} images are missing under the dataset root.")
        print(f"   dataset root : {dataset}")
        print(f"   first missing: {images[absent[0]]}")
        if not dataset.exists():
            print("   -> that root does not exist. Fix paths.dataset in config.yaml.")
        else:
            print("   -> the root exists but the files do not. Check the images/ subfolder.")
        return 2

    run_id = a.run_id or f"{pathlib.Path(a.plan).stem}-{time.strftime('%Y%m%d-%H%M%S')}"
    if a.concurrency:
        cfg["run"]["concurrency"] = a.concurrency
    runner = Runner(cfg, [x.strip().upper() for x in a.arms.split(",")], run_id,
                    stage_timeouts={"extract": a.extract_timeout, "judge": a.judge_timeout})

    carried: List[DocResult] = []
    if a.resume:
        keep = []
        for d in doc_ids:
            f = runner.out / "raw" / f"{d}.json"
            if f.exists():
                prev = json.loads(f.read_text())
                if prev.get("ok") and set(runner.arms) <= set((prev.get("arms") or {})):
                    carried.append(DocResult(**{k: v for k, v in prev.items()
                                                if k in DocResult.__dataclass_fields__}))
                    continue
            keep.append(d)
        print(f"resume: {len(carried)} already complete, {len(keep)} to run\n")
        doc_ids = keep

    try:
        results = (carried + runner.run_plan(doc_ids, images, a.doc_timeout)) if doc_ids \
            else carried
    except SpendCapExceeded as exc:
        print(f"\n!! {exc}")
        results = []
        for f in sorted((runner.out / "raw").glob("*.json")):
            d = json.loads(f.read_text())
            results.append(DocResult(**{k: v for k, v in d.items()
                                        if k in DocResult.__dataclass_fields__}))
        runner.manifest(results, plan, {"aborted": str(exc)})
        return 3

    m = runner.manifest(results, plan)
    print(f"\n  ok {m['n_ok']}/{m['n_run']}  failed {m['n_failed']}"
          f"  cached {m['n_cached']}"
          + (f"  timed out {m['n_timed_out']}" if m["n_timed_out"] else ""))
    if m["stage_failures"]:
        print(f"  stage failures {m['stage_failures']}")
    print(f"  cost ${m['total_cost_usd']:.4f}  (${m['cost_per_doc_usd']:.5f}/doc)")
    print(f"  tokens {m['tokens']}")
    print(f"  -> {runner.out}")
    if _LEAKED:
        print(f"\n  !! {len(_LEAKED)} stage thread(s) abandoned mid-call and cannot be "
              f"cancelled:\n     " + "\n     ".join(sorted(_LEAKED)[:10]))
        print("     They are daemons, so the process still exits. Their tokens are spent but\n"
              "     unaccounted: the true cost of this run is slightly higher than reported.")
    sys.stdout.flush()
    if m["n_failed"]:
        # An abandoned daemon thread cannot keep the interpreter alive, but a non-daemon
        # thread inside the product could, so exit hard rather than risk hanging at exit.
        os._exit(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
