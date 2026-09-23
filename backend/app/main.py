"""FastAPI application: ICT × Jev × Deriv platform (FX Scalper Community fork)."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import config, db, events
from app.routers import legacy, market_data, system

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("fxs")

UI_DIST = config.PROJECT_DIR / "ui" / "dist"
OPTIONAL_ROUTERS = ("transcripts", "strategy", "backtests", "trading", "metrics", "webhooks")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    events.bind_loop(asyncio.get_running_loop())
    if importlib.util.find_spec("openbb") is not None and not config.env_bool("COMMUNITY_NO_OPENBB_WARM"):
        from app.services.data import openbb_feed
        openbb_feed.warm()
    log.info("API on http://%s:%s  (docs at /docs)", config.host(), config.port())
    yield
    try:
        from app.services import engine
        await engine.stop(reason="shutdown")
    except ImportError:
        pass


app = FastAPI(
    title="FX Scalper — ICT × Jev × Deriv",
    version="0.2.0",
    summary="Transcripts → Jev-tagged ICT concepts → typed strategy graph → Deriv backtests and demo execution.",
    lifespan=lifespan,
)

# Same-origin in production (FastAPI serves ui/dist). CORS only for local dev servers.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(legacy.router)
app.include_router(system.router)
app.include_router(market_data.router)
for name in OPTIONAL_ROUTERS:
    if importlib.util.find_spec(f"app.routers.{name}") is not None:
        module = importlib.import_module(f"app.routers.{name}")
        app.include_router(module.router)

if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=Path(UI_DIST), html=True), name="ui")
