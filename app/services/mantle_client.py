"""Mantle RPC client.

Wraps web3.py with:
  - POA middleware (OP-stack chains use extradata that breaks default parsers)
  - Retry on transient failures (network blips, RPC rate limits)
  - Type-safe block timestamp helper
"""

from __future__ import annotations

from datetime import datetime, timezone

from tenacity import retry, stop_after_attempt, wait_exponential
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from app.core.config import Settings
from app.core.logging_setup import get_logger

log = get_logger(__name__)


class MantleClient:
    """Thin sync wrapper over Web3. Used by the polling task.

    Note: web3.py is synchronous. We call it from an async background task,
    which is fine because the calls are short and the task isn't serving HTTP
    requests. If RPC latency ever becomes a problem, we'd move to AsyncWeb3.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.w3 = Web3(
            Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 30})
        )
        # OP-stack chains include extradata fields that the default middleware
        # can't parse. This middleware tolerates them.
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def assert_connected(self) -> None:
        """Validate connection AND chain ID. Call once at startup."""
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot reach RPC at {self.settings.rpc_url}")
        actual = self.w3.eth.chain_id
        if actual != self.settings.chain_id:
            raise ValueError(
                f"Chain ID mismatch: expected {self.settings.chain_id} "
                f"({self.settings.mantle_network}), RPC returned {actual}"
            )
        log.info(
            "rpc_connected",
            network=self.settings.mantle_network,
            chain_id=actual,
            rpc=self.settings.rpc_url,
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_block_number(self) -> int:
        return self.w3.eth.block_number

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def get_block_timestamp(self, block_number: int | str = "latest") -> datetime:
        block = self.w3.eth.get_block(block_number)
        return datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)

    def contract(self, address: str, abi: list):
        """Build a web3 contract object with checksummed address."""
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
