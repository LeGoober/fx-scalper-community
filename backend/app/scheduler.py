"""Periodic background jobs (calendar, news), started with the app.

Each job runs in a worker thread (the OpenBB/HTTP calls are blocking), never overlaps itself, and
records its last run and error so the dashboard can show data freshness. A failing job only logs;
it never takes the API down. Set COMMUNITY_NO_SCHEDULER=1 to disable (tests do).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from app import config, events

log = logging.getLogger("fxs.scheduler")


@dataclass
class Periodic:
    name: str
    interval_s: float
    fn: Callable[[], dict | None]
    enabled: Callable[[], bool] = lambda: True
    first_delay_s: float = 5.0
    last_run: float | None = None
    last_ok: float | None = None
    last_error: str | None = None
    last_result: dict | None = None
    runs: int = 0
    _task: asyncio.Task | None = field(default=None, repr=False)

    async def run_once(self) -> None:
        if not self.enabled():
            return
        self.last_run = time.time()
        self.runs += 1
        try:
            self.last_result = await asyncio.to_thread(self.fn)
            self.last_ok, self.last_error = time.time(), None
        except Exception as exc:  # recorded, never fatal
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.warning("job %s failed: %s", self.name, self.last_error)
            events.publish("data.job_error", {"job": self.name, "error": self.last_error}, level="warning",
                           message=f"Data job {self.name} failed")

    async def _loop(self) -> None:
        await asyncio.sleep(self.first_delay_s)
        while True:
            await self.run_once()
            await asyncio.sleep(self.interval_s)

    def status(self) -> dict:
        return {"name": self.name, "interval_s": self.interval_s, "enabled": self.enabled(), "runs": self.runs,
                "last_run": self.last_run, "last_ok": self.last_ok, "last_error": self.last_error,
                "last_result": self.last_result, "running": bool(self._task and not self._task.done())}


def _calendar_job() -> dict:
    from app.services.data import openbb_feed
    out = openbb_feed.calendar(days_back=1, days_ahead=7, min_importance=1)
    return {"provider": out["provider"], "fetched": out["fetched"], "fallback_reason": out["fallback_reason"]}


def _news_job() -> dict:
    from app.services.data import openbb_feed
    out = openbb_feed.news("forex", limit=30)
    return {"provider": out["provider"], "items": len(out["items"]), "new": out.get("new", 0)}


def _openbb_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("openbb") is not None


JOBS: list[Periodic] = [
    Periodic("calendar", 15 * 60, _calendar_job),
    Periodic("news", 5 * 60, _news_job, enabled=_openbb_installed, first_delay_s=20),
]


def start() -> None:
    if config.env_bool("COMMUNITY_NO_SCHEDULER"):
        return
    loop = asyncio.get_running_loop()
    for job in JOBS:
        if job._task is None or job._task.done():
            job._task = loop.create_task(job._loop(), name=f"scheduler:{job.name}")


async def stop() -> None:
    for job in JOBS:
        if job._task:
            job._task.cancel()
            job._task = None


def status() -> list[dict]:
    return [j.status() for j in JOBS]
