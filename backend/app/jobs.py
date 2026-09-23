"""Background jobs (backfill, transcript pulls, tagging, backtests) as asyncio tasks.

Progress is streamed as `job.progress` events. Only start and finish go to the
event log (see events.PERSISTED_PREFIXES handling below).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

from app import db, events

log = logging.getLogger("fxs.jobs")

JobFn = Callable[["Job"], Awaitable[Any]]
_jobs: dict[str, "Job"] = {}
MAX_KEPT = 200


class Job:
    def __init__(self, kind: str, params: dict) -> None:
        self.id = f"{kind}-{uuid.uuid4().hex[:10]}"
        self.kind = kind
        self.params = params
        self.status = "queued"  # queued | running | done | failed | cancelled
        self.progress: dict = {}
        self.result: Any = None
        self.error: str | None = None
        self.created_at = db.utc_now()
        self.finished_at: str | None = None
        self.task: asyncio.Task | Future | None = None  # Task, or concurrent Future when started from a thread

    def update(self, **progress: Any) -> None:
        self.progress.update(progress)
        # `stream.` prefix is not persisted: progress ticks are live-only.
        events.publish("stream.job.progress", self.to_dict())

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "params": self.params,
                "progress": self.progress, "result": self.result, "error": self.error,
                "created_at": self.created_at, "finished_at": self.finished_at}


def start(kind: str, params: dict, fn: JobFn) -> Job:
    job = Job(kind, params)
    _jobs[job.id] = job
    if len(_jobs) > MAX_KEPT:
        for old in [j for j in _jobs.values() if j.status in {"done", "failed", "cancelled"}][: len(_jobs) - MAX_KEPT]:
            _jobs.pop(old.id, None)

    async def runner() -> None:
        job.status = "running"
        events.publish("job.started", job.to_dict(), message=f"{kind} started")
        try:
            job.result = await fn(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:  # surfaced to the UI, never swallowed
            log.exception("job %s failed", job.id)
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        job.finished_at = db.utc_now()
        level = "error" if job.status == "failed" else "info"
        events.publish(f"job.{job.status}", job.to_dict(), level=level, message=f"{kind} {job.status}")

    # Sync FastAPI endpoints run in a worker thread with no event loop; schedule onto the app loop.
    try:
        job.task = asyncio.get_running_loop().create_task(runner())
    except RuntimeError:
        loop = events._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("No application event loop to run background jobs on.")
        job.task = asyncio.run_coroutine_threadsafe(runner(), loop)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs(kind: str | None = None) -> list[dict]:
    items = [j.to_dict() for j in _jobs.values() if kind is None or j.kind == kind]
    return sorted(items, key=lambda j: j["created_at"], reverse=True)


def cancel(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job and job.task and not job.task.done():
        job.task.cancel()
        return True
    return False
