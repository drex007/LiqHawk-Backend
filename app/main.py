"""FastAPI app entry point.

Run with:
    uvicorn app.main:app --reload

What happens at startup (lifespan):
  1. Configure logging
  2. Connect to Mongo (and create indexes)
  3. Validate INIT addresses
  4. Start the background polling task

What happens at shutdown:
  1. Stop the polling task gracefully
  2. Close Mongo connection
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import cascade, health, positions, snapshots
from app.core.config import get_settings
from app.core.logging_setup import configure_logging, get_logger
from app.db.mongo import mongo
from app.services import pipeline as pipeline_module
from app.services.pipeline import CascadePoller

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI startup/shutdown hook.

    Anything before `yield` runs at startup.
    Anything after runs at shutdown.
    """
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)
    log.info("app_starting", network=settings.mantle_network)

    # 1. Mongo
    await mongo.connect(settings)

    # 2. Validate INIT addresses (fails loudly if Sepolia placeholders are empty)
    settings.init_addresses.validate(settings.mantle_network)

    # 3. Background poller
    assert mongo.db is not None
    pipeline_module.poller = CascadePoller(settings, mongo.db)
    pipeline_module.poller.start()

    yield  # ---- app runs here ----

    # Shutdown — reverse order
    log.info("app_stopping")
    if pipeline_module.poller:
        await pipeline_module.poller.stop()
    await mongo.disconnect()


app = FastAPI(
    title="Liquidation Cascade Detector",
    description="Monitors INIT Capital on Mantle for liquidation cascade risk.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — open to any origin since this API is read-only and exposes no
# secrets. Tighten `allow_origins` to specific hostnames if you ever wrap
# write endpoints (e.g., a backend that triggers ManualSnapshot).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Routes
app.include_router(health.router)
app.include_router(snapshots.router)
app.include_router(positions.router)
app.include_router(cascade.router)


@app.get("/")
async def root() -> dict:
    """Minimal landing — points clients to the docs."""
    return {
        "name": "Liquidation Cascade Detector",
        "docs": "/docs",
        "health": "/health",
    }
