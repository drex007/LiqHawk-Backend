"""MongoDB connection management.

Why async (motor) instead of sync (pymongo):
  FastAPI runs on an asyncio event loop. A sync DB call blocks that loop
  and prevents OTHER requests from being served. With motor, all DB calls
  are awaitable — the loop stays free.

Pattern:
  - One MongoClient for the whole app lifetime (connection pool inside)
  - Attached to app.state during startup, torn down during shutdown
  - Handlers access it via the get_db() dependency
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING

from app.core.config import Settings
from app.core.logging_setup import get_logger

log = get_logger(__name__)


class MongoManager:
    """Wraps the motor client and exposes the database.

    Stored on app.state at startup. Don't instantiate elsewhere.
    """

    def __init__(self) -> None:
        self.client: AsyncIOMotorClient | None = None
        self.db: AsyncIOMotorDatabase | None = None

    async def connect(self, settings: Settings) -> None:
        """Open the connection pool. Called from FastAPI lifespan startup."""
        self.client = AsyncIOMotorClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5000,  # fail fast if Mongo is down
        )
        # `ping` forces an actual connection attempt — without it, errors hide
        # until the first real query, which makes startup failures confusing.
        await self.client.admin.command("ping")
        self.db = self.client[settings.mongo_db_name]
        log.info("mongo_connected", uri=settings.mongo_uri, db=settings.mongo_db_name)

        await self._ensure_indexes()

    async def disconnect(self) -> None:
        """Close the pool. Called from FastAPI lifespan shutdown."""
        if self.client:
            self.client.close()
            log.info("mongo_disconnected")

    async def _ensure_indexes(self) -> None:
        """Create indexes if they don't exist. Idempotent — safe on every boot.

        Why these indexes:
          - snapshots.block_number DESC: "give me the latest snapshot" is the hottest query
          - snapshots.captured_at DESC: time-range queries for the API
          - positions.position_id + captured_at: position history queries
          - positions.risk_level + captured_at: "show me all CRITICAL positions right now"
        """
        assert self.db is not None

        await self.db.snapshots.create_index([("block_number", DESCENDING)])
        await self.db.snapshots.create_index([("captured_at", DESCENDING)])

        await self.db.positions.create_index(
            [("position_id", ASCENDING), ("captured_at", DESCENDING)]
        )
        await self.db.positions.create_index(
            [("risk_level", ASCENDING), ("captured_at", DESCENDING)]
        )
        await self.db.positions.create_index([("captured_at", DESCENDING)])
        # Multi-protocol support — quickly filter by protocol
        await self.db.positions.create_index(
            [("protocol", ASCENDING), ("captured_at", DESCENDING)]
        )

        log.info("mongo_indexes_ready")


# Module-level singleton — there's exactly one connection pool per process.
mongo = MongoManager()


def get_db() -> AsyncIOMotorDatabase:
    """FastAPI dependency — handlers do: db: AsyncIOMotorDatabase = Depends(get_db)."""
    if mongo.db is None:
        raise RuntimeError("Mongo not connected. Did the lifespan startup run?")
    return mongo.db
