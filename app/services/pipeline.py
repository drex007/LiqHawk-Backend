"""Background polling task.

This is what runs in parallel with the FastAPI HTTP server. The asyncio task
is created during the lifespan startup and cancelled during shutdown.

Loop logic:
  1. Read the current INIT state (sync web3 call, wrapped in run_in_executor
     so it doesn't block the event loop)
  2. Build a PoolSnapshot
  3. Persist to Mongo
  4. Sleep `poll_interval_seconds`
  5. Repeat
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import Settings
from app.core.logging_setup import get_logger
from app.core.schema import PoolSnapshot
from app.db import repositories as repo
from app.services.alerts import AlertPublisher
from app.services.cascade import detect_zones
from app.services.init_capital import InitCapitalReader
from app.services.lendle import LendleReader
from app.services.mantle_client import MantleClient
from app.services.on_chain_logger import OnChainLogger
from app.services.protocol_reader import LendingProtocolReader

log = get_logger(__name__)


class CascadePoller:
    """Owns the polling loop and tracks state for graceful shutdown."""

    def __init__(self, settings: Settings, db: AsyncIOMotorDatabase):
        self.settings = settings
        self.db = db
        self.client = MantleClient(settings)
        # `self.reader` is kept for backward-compat (tests reference it) and
        # points at the INIT reader. `self.readers` is the list the poll loop
        # actually iterates — pluggable per protocol via settings.
        self.reader = InitCapitalReader(self.client, settings)
        self.readers: list[LendingProtocolReader] = [self.reader]
        if settings.enable_lendle:
            self.readers.append(LendleReader(self.client, settings))
        self.alerts = AlertPublisher(
            webhook_url=settings.discord_webhook_url or None,
            dedup_window_seconds=settings.alert_dedup_window_seconds,
            telegram_bot_token=settings.telegram_bot_token or None,
            telegram_chat_id=settings.telegram_chat_id or None,
        )
        # Cycle counter — drives the per-poll terminal notification
        self._cycle = 0
        # On-chain logger is optional — disabled unless both deployer key and
        # contract address are configured. The OnChainLogger constructor itself
        # checks `enabled`; setting `None` here when the ABI file is missing
        # keeps unit tests from caring about contract artifacts.
        try:
            logger = OnChainLogger(settings)
            self.on_chain_logger = logger if logger.enabled else None
        except FileNotFoundError as e:
            log.warning("on_chain_logger_unavailable", reason=str(e))
            self.on_chain_logger = None
        # Last-seen risk levels per position id — drives "new CRITICAL" alerts
        self._last_risk: dict[str, str] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- Lifecycle ---

    def start(self) -> None:
        """Schedule the polling loop on the running event loop."""
        if self._task is not None:
            log.warning("poller_already_started")
            return
        self.client.assert_connected()
        self._task = asyncio.create_task(self._run(), name="cascade-poller")
        log.info("poller_started", interval=self.settings.poll_interval_seconds)

    async def stop(self) -> None:
        """Signal the loop to exit and await its termination."""
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
            log.info("poller_stopped")
        await self.alerts.aclose()

    # --- Single-snapshot path (also called by the diagnostics route) ---

    async def take_snapshot(self) -> PoolSnapshot:
        """Read INIT state once and return a snapshot — no DB writes.

        Runs the sync web3 calls in a thread pool so the event loop stays
        responsive to HTTP requests during the read.
        """
        loop = asyncio.get_running_loop()
        max_positions = self.settings.max_positions_per_cycle or None

        block_number = await loop.run_in_executor(None, self.client.get_block_number)
        block_timestamp = await loop.run_in_executor(
            None, self.client.get_block_timestamp, block_number
        )

        # Read every configured protocol sequentially in the executor.
        # We run them serially (not gather) to avoid hammering the public RPC.
        positions = []
        for reader in self.readers:
            try:
                got = await loop.run_in_executor(
                    None, reader.read_all_positions, max_positions
                )
                positions.extend(got)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "reader_failed",
                    protocol=getattr(reader, "protocol_name", "?"),
                    err=str(e),
                )

        return PoolSnapshot(
            block_number=block_number,
            block_timestamp=block_timestamp,
            chain_id=self.settings.chain_id,
            network=self.settings.mantle_network,
            positions=positions,
            captured_at=datetime.now(timezone.utc),
        )

    # --- The loop itself ---

    async def _run(self) -> None:
        while not self._stop.is_set():
            self._cycle += 1
            cycle_start = datetime.now(timezone.utc)
            try:
                snapshot = await self.take_snapshot()
                await repo.save_snapshot(self.db, snapshot)
                zone_count = await self._handle_alerts_and_logging(snapshot)
                self._print_cycle_summary(snapshot, zone_count, cycle_start)
            except Exception as e:  # noqa: BLE001 — never let one cycle kill the loop
                log.error("poll_cycle_failed", err=str(e), exc_info=True)
                print(f"[poll #{self._cycle}] FAILED — {type(e).__name__}: {str(e)[:120]}",
                      flush=True)

            # asyncio.wait_for + Event lets us interrupt the sleep on shutdown
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass  # normal — means it's time to poll again

    async def _handle_alerts_and_logging(self, snapshot: PoolSnapshot) -> int:
        """Run cascade detection, fire alerts, and (later) log on-chain.

        Kept isolated from the storage path so a Discord outage can't lose
        a snapshot — `save_snapshot` ran first. Returns the zone count so
        the per-cycle terminal summary can include it.
        """
        zones = detect_zones(
            snapshot.positions,
            at_risk_hf=Decimal(str(self.settings.cascade_at_risk_hf)),
            min_cluster_size=self.settings.cascade_min_cluster_size,
        )

        # Send cascade-zone alerts (deduplicated inside the publisher)
        await self.alerts.publish_cascade_zones(
            zones, block_number=snapshot.block_number
        )

        # Send per-position risk-escalation alerts (MEDIUM/HIGH/CRITICAL crossings)
        await self.alerts.publish_new_at_risk_positions(
            previous=self._last_risk,
            current=snapshot.positions,
            block_number=snapshot.block_number,
            min_severity=self.settings.alert_min_severity,
        )
        self._last_risk = {p.position_id: p.risk_level for p in snapshot.positions}

        # On-chain logging hook (Phase 4) — only if logger configured
        if self.on_chain_logger is not None and zones:
            try:
                await self.on_chain_logger.log_zones(
                    zones, block_number=snapshot.block_number
                )
            except Exception as e:  # noqa: BLE001
                log.warning("on_chain_log_failed", err=str(e))
        return len(zones)

    def _print_cycle_summary(
        self, snapshot: PoolSnapshot, zone_count: int, started_at: datetime
    ) -> None:
        """One unmissable terminal line per successful poll."""
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        at_risk = sum(
            1 for p in snapshot.positions if p.risk_level in ("HIGH", "CRITICAL")
        )
        critical = sum(1 for p in snapshot.positions if p.risk_level == "CRITICAL")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(
            f"[{ts}] poll #{self._cycle:<4d} "
            f"block={snapshot.block_number} "
            f"positions={len(snapshot.positions):<5d} "
            f"at_risk={at_risk:<3d} critical={critical:<3d} "
            f"zones={zone_count:<2d} "
            f"({elapsed:5.1f}s)",
            flush=True,
        )


# Module-level singleton, populated during lifespan startup
poller: CascadePoller | None = None


def get_poller() -> CascadePoller:
    """FastAPI dependency for routes that need to trigger a manual snapshot."""
    if poller is None:
        raise RuntimeError("Poller not started. Did the lifespan startup run?")
    return poller
