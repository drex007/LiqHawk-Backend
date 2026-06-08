"""Discord + Telegram alert publisher.

Two kinds of alerts:

  1. Cascade-zone alerts — fire when detect_zones() produces a new or
     escalating zone. Rich embed with severity, positions, time-to-cascade.

  2. Position-level alerts — fire when individual positions cross into
     CRITICAL risk (HF < 1.0) since the previous snapshot. Lower priority
     but useful for ops teams watching specific addresses.

Discord and Telegram receive the SAME alert set. The embed dict is the
canonical structured form; we render it once for each transport. Either
channel can be disabled independently by leaving its credentials blank.

Deduplication: an in-process LRU set tracks recently-alerted fingerprints
so a steady-state cascade doesn't spam the channel every 30 seconds.
Dedup is shared across channels — one fingerprint, both transports skip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

import httpx

from app.core.logging_setup import get_logger
from app.core.schema import Position
from app.services.cascade import CascadeZone, estimate_time_to_cascade_minutes

log = get_logger(__name__)


# --- Severity → Discord embed color (decimal RGB) ----------------------------
SEVERITY_COLORS = {
    "CRITICAL": 0xE74C3C,   # red
    "HIGH": 0xE67E22,       # orange
    "MEDIUM": 0xF1C40F,     # yellow
    "LOW": 0x95A5A6,        # grey
}


@dataclass
class _DedupEntry:
    fingerprint: str
    severity: str
    seen_at: datetime


class AlertPublisher:
    """Posts cascade and position alerts to Discord and/or Telegram.

    Construction is cheap (no I/O). Pass `webhook_url=None` to disable
    Discord; leave `telegram_bot_token` blank to disable Telegram. With both
    disabled, the publisher logs what it *would* have sent — useful in dev/CI.
    """

    def __init__(
        self,
        webhook_url: str | None,
        *,
        dedup_window_seconds: int = 600,
        http_client: httpx.AsyncClient | None = None,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
        # Telegram bot API allows ~1 msg/sec sustained per channel. We default
        # to 1.1s between sends to stay safely under the limit even when a
        # single poll detects many zones at once.
        send_gap_seconds: float = 1.1,
    ):
        self.webhook_url = webhook_url or None
        self.telegram_bot_token = telegram_bot_token or None
        self.telegram_chat_id = telegram_chat_id or None
        self.dedup_window = timedelta(seconds=dedup_window_seconds)
        self.send_gap_seconds = send_gap_seconds
        self._last_send_ts: float = 0.0  # monotonic clock — for throttling
        # Injectable client lets tests pass a mock without monkey-patching httpx.
        self._client = http_client
        self._owned_client = http_client is None
        self._seen: dict[str, _DedupEntry] = {}

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def discord_enabled(self) -> bool:
        return self.webhook_url is not None

    async def aclose(self) -> None:
        if self._owned_client and self._client is not None:
            await self._client.aclose()

    # --- Public API ----------------------------------------------------------

    async def publish_cascade_zones(
        self,
        zones: Sequence[CascadeZone],
        *,
        block_number: int,
    ) -> int:
        """Publish alerts for every zone that's new or has escalated.

        Returns the count of alerts actually sent (after dedup).
        """
        if not zones:
            return 0
        sent = 0
        for zone in zones:
            if not self._should_alert_zone(zone):
                continue
            await self._post_embed(self._zone_embed(zone, block_number))
            sent += 1
        if sent:
            log.info("alerts_sent", kind="cascade_zone", count=sent)
        return sent

    async def publish_new_critical_positions(
        self,
        previous: dict[str, str],
        current: Sequence[Position],
        *,
        block_number: int,
    ) -> int:
        """Alert on positions that crossed into CRITICAL since last snapshot.

        `previous` maps position_id → prior risk_level. Positions absent from
        the previous map are treated as new — alert if currently CRITICAL.
        """
        sent = 0
        for pos in current:
            if pos.risk_level != "CRITICAL":
                continue
            prior = previous.get(pos.position_id)
            if prior == "CRITICAL":
                continue  # was already critical — already alerted last cycle
            fp = f"position:{pos.position_id}:CRITICAL"
            if not self._dedup_admit(fp, "CRITICAL"):
                continue
            await self._post_embed(self._position_embed(pos, block_number))
            sent += 1
        if sent:
            log.info("alerts_sent", kind="new_critical", count=sent)
        return sent

    # --- Embed builders ------------------------------------------------------

    def _zone_embed(self, zone: CascadeZone, block_number: int) -> dict:
        eta_min = estimate_time_to_cascade_minutes(zone)
        prob_pct = zone.probability * 100

        # Build the top-N most-critical address listing. zone.positions is
        # already sorted by HF ascending. Discord field values max at 1024
        # chars — we cap at 8 entries (~70 chars each) to stay well under.
        TOP_N = 8
        top = zone.positions[:TOP_N]
        lines = []
        for p in top:
            owner_short = f"{p.owner[:6]}…{p.owner[-4:]}"
            lines.append(
                f"`{owner_short}` HF={float(p.health_factor):.3f} "
                f"debt=${float(p.total_debt_usd):,.0f} [{p.protocol}]"
            )
        if zone.num_positions > TOP_N:
            lines.append(f"…and {zone.num_positions - TOP_N} more")
        addresses_field = "\n".join(lines) or "—"

        return {
            "title": f"⚠️ Liquidation cascade detected — {zone.collateral_symbol}",
            "description": (
                f"**{zone.num_positions}** positions sharing **{zone.collateral_symbol}** "
                f"collateral are clustered near liquidation. Estimated **{eta_min} min** "
                f"to cascade if price trend continues."
            ),
            "color": SEVERITY_COLORS.get(zone.severity, 0x95A5A6),
            "fields": [
                {"name": "Severity", "value": zone.severity, "inline": True},
                {"name": "Probability", "value": f"{prob_pct:.1f}%", "inline": True},
                {"name": "Anomaly flags", "value": str(zone.anomaly_count), "inline": True},
                {"name": "Positions at risk", "value": str(zone.num_positions), "inline": True},
                {"name": "Total debt", "value": f"${float(zone.total_debt_usd):,.0f}", "inline": True},
                {"name": "Total collateral", "value": f"${float(zone.total_collateral_usd):,.0f}", "inline": True},
                {"name": "Avg HF", "value": f"{float(zone.avg_health_factor):.3f}", "inline": True},
                {"name": "Min HF", "value": f"{float(zone.min_health_factor):.3f}", "inline": True},
                {"name": "Block", "value": str(block_number), "inline": True},
                {"name": f"Top {min(zone.num_positions, TOP_N)} at-risk addresses",
                 "value": addresses_field, "inline": False},
            ],
            "footer": {"text": "Liquidation Cascade Detector — INIT Capital on Mantle"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _position_embed(self, pos: Position, block_number: int) -> dict:
        return {
            "title": "\U0001F525 Position entered CRITICAL risk",
            "description": (
                f"Position `{pos.position_id[:20]}...` owned by `{pos.owner}` "
                f"is now liquidatable (HF = {float(pos.health_factor):.4f})."
            ),
            "color": SEVERITY_COLORS["CRITICAL"],
            "fields": [
                {"name": "Health factor", "value": f"{float(pos.health_factor):.4f}", "inline": True},
                {"name": "Dominant collateral",
                 "value": pos.dominant_collateral or "—", "inline": True},
                {"name": "Debt", "value": f"${float(pos.total_debt_usd):,.0f}", "inline": True},
                {"name": "Collateral", "value": f"${float(pos.total_collateral_usd):,.0f}", "inline": True},
                {"name": "Block", "value": str(block_number), "inline": True},
            ],
            "footer": {"text": "Liquidation Cascade Detector — INIT Capital on Mantle"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # --- Dedup logic ---------------------------------------------------------

    def _zone_fingerprint(self, zone: CascadeZone) -> str:
        # Two zones share a fingerprint iff they share collateral AND severity.
        # An UPGRADE in severity (HIGH → CRITICAL) defeats dedup and re-alerts.
        return f"zone:{zone.collateral_symbol}:{zone.severity}"

    def _should_alert_zone(self, zone: CascadeZone) -> bool:
        return self._dedup_admit(self._zone_fingerprint(zone), zone.severity)

    def _dedup_admit(self, fp: str, severity: str) -> bool:
        """Returns True if this fingerprint should be alerted; False if dedup'd."""
        now = datetime.now(timezone.utc)
        self._evict_expired(now)
        prev = self._seen.get(fp)
        if prev is not None:
            return False
        self._seen[fp] = _DedupEntry(fingerprint=fp, severity=severity, seen_at=now)
        return True

    def _evict_expired(self, now: datetime) -> None:
        cutoff = now - self.dedup_window
        stale = [fp for fp, e in self._seen.items() if e.seen_at < cutoff]
        for fp in stale:
            del self._seen[fp]

    # --- HTTP ---------------------------------------------------------------

    async def _post_embed(self, embed: dict) -> None:
        """Fan out one alert to every enabled transport (Discord + Telegram).

        Errors on one transport never block the other — alerting is best-effort.
        If no transport is configured we log the alert and move on so the rest
        of the pipeline keeps running.

        Throttling: when many zones fire in a single poll cycle we'd otherwise
        burst-post and trip Telegram's per-channel rate limit (~1 msg/sec).
        `_await_throttle` enforces a minimum gap between sends across the
        publisher's lifetime, sized for Telegram (Discord webhooks are more
        forgiving but the same gap is fine for both).
        """
        if not self.discord_enabled and not self.telegram_enabled:
            log.info("alert_skipped_no_transport", title=embed.get("title", ""))
            return

        await self._await_throttle()

        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            if self.discord_enabled:
                await self._post_discord(client, embed)
            if self.telegram_enabled:
                await self._post_telegram(client, embed)
        finally:
            if self._client is None:  # ephemeral, owned-here client
                await client.aclose()

    async def _await_throttle(self) -> None:
        """Sleep until at least `send_gap_seconds` have passed since the last send."""
        if self.send_gap_seconds <= 0:
            return
        loop = asyncio.get_event_loop()
        now = loop.time()
        wait = self.send_gap_seconds - (now - self._last_send_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_send_ts = loop.time()

    async def _post_discord(self, client: httpx.AsyncClient, embed: dict) -> None:
        try:
            r = await client.post(self.webhook_url, json={"embeds": [embed]})  # type: ignore[arg-type]
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("discord_post_failed", err=str(e))

    # Cap on retry sleep — if Telegram asks for >60s we just drop the alert
    # rather than block the publisher (the pipeline keeps running).
    _MAX_RETRY_AFTER = 60

    async def _post_telegram(self, client: httpx.AsyncClient, embed: dict) -> None:
        """Send to Telegram. On 429, honour `retry_after` and retry once."""
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": self.telegram_chat_id,
            "text": self._embed_to_html(embed),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in (1, 2):
            try:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                return
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt == 1:
                    retry_after = self._parse_retry_after(e.response)
                    if retry_after <= self._MAX_RETRY_AFTER:
                        log.info("telegram_rate_limited", retry_after=retry_after)
                        await asyncio.sleep(retry_after + 0.5)
                        # Throttle window slides too, so the next caller waits.
                        self._last_send_ts = asyncio.get_event_loop().time()
                        continue
                log.warning(
                    "telegram_post_failed",
                    err=str(e),
                    body=e.response.text[:300],
                )
                return
            except httpx.HTTPError as e:
                log.warning("telegram_post_failed", err=str(e))
                return

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> int:
        """Telegram returns retry seconds in the JSON body, not the header."""
        try:
            data = response.json()
            return int(data.get("parameters", {}).get("retry_after", 1))
        except (ValueError, KeyError, TypeError):
            return 1

    # --- Telegram rendering --------------------------------------------------
    #
    # HTML parse mode is strictly easier than Markdown(V1) — only <, >, & need
    # escaping, and entity boundaries are explicit tags so usernames with
    # underscores (e.g. @liqhawk_alerts) and dashes can't break the parser.

    @staticmethod
    def _esc(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @classmethod
    def _embed_to_html(cls, embed: dict) -> str:
        """Render the Discord embed dict as a Telegram HTML message.

        The embed shape is the canonical source; both transports read from it.
        Code-fenced segments (backticks in the embed) become <code>…</code>.
        """
        lines: list[str] = []

        title = embed.get("title")
        if title:
            lines.append(f"<b>{cls._render_inline(title)}</b>")

        description = embed.get("description")
        if description:
            lines.append("")
            lines.append(cls._render_inline(description))

        fields = embed.get("fields") or []
        if fields:
            lines.append("")
            for f in fields:
                name = f.get("name", "")
                value = f.get("value", "")
                if not name and not value:
                    continue
                # Multi-line field values (like the top-N addresses list) go on
                # their own block; single-line values stay inline as "name: value".
                if "\n" in str(value):
                    lines.append(f"<b>{cls._esc(name)}</b>")
                    lines.append(cls._render_inline(str(value)))
                else:
                    lines.append(
                        f"<b>{cls._esc(name)}:</b> {cls._render_inline(str(value))}"
                    )

        footer = (embed.get("footer") or {}).get("text")
        if footer:
            lines.append("")
            lines.append(f"<i>{cls._esc(footer)}</i>")

        return "\n".join(lines)

    @classmethod
    def _render_inline(cls, text: str) -> str:
        """Convert markdown-ish inline markers in embed text to HTML.

        Embeds use backticks for monospace and **bold** for emphasis — we
        translate those to <code> and <b> while escaping everything else.
        Anything weird falls through as plain text rather than 400'ing.
        """
        import re

        out_parts: list[str] = []
        i = 0
        s = str(text)
        # Match either `code` or **bold** — first hit wins.
        pattern = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*")
        for m in pattern.finditer(s):
            if m.start() > i:
                out_parts.append(cls._esc(s[i : m.start()]))
            code, bold = m.group(1), m.group(2)
            if code is not None:
                out_parts.append(f"<code>{cls._esc(code)}</code>")
            else:
                out_parts.append(f"<b>{cls._esc(bold)}</b>")
            i = m.end()
        if i < len(s):
            out_parts.append(cls._esc(s[i:]))
        return "".join(out_parts)
