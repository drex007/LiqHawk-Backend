"""Lendle (Aave V2 fork on Mantle) reader.

Verified live against Mantle mainnet 2026-05-18:
  - LendingPool       0xCFa5aE7c2CE8Fadc6426C1ff872cA45378Fb7cF3
  - AddressesProvider 0xAb94Bedd21ae3411eB2698945dfCab1D5C19C3d4

Aave V2 has no on-chain user enumeration — to discover borrowers we scrape
historical `Borrow(reserve, user, onBehalfOf, amount, ...)` events from a
recent block window. The set of unique `onBehalfOf` addresses is the
candidate borrower pool; we then read `getUserAccountData` per address.

Math:
  Aave V2 returns healthFactor in 1e18 fixed point. A user with no debt
  gets `type(uint256).max` — we sentinel that to Decimal("999") to match
  INIT's convention.

  Collateral/debt are returned in **ETH** units (legacy from Ethereum Aave).
  On Lendle, the reference asset is the protocol's price-feed currency (USD
  for stablecoin-denominated pools). We treat the values as USD directly —
  Lendle's docs confirm this for the Mantle deployment.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from web3 import Web3
from web3.exceptions import ContractLogicError

from app.core.config import Settings
from app.core.logging_setup import get_logger
from app.core.schema import Position
from app.services.mantle_client import MantleClient

log = get_logger(__name__)


# --- Contract addresses on Mantle mainnet ----------------------------------

LENDLE_LENDING_POOL = "0xCFa5aE7c2CE8Fadc6426C1ff872cA45378Fb7cF3"
LENDLE_ADDRESSES_PROVIDER = "0xAb94Bedd21ae3411eB2698945dfCab1D5C19C3d4"


# --- ABIs (minimal) ---------------------------------------------------------

LENDING_POOL_ABI: list = [
    {
        "name": "getUserAccountData",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralETH", "type": "uint256"},
            {"name": "totalDebtETH", "type": "uint256"},
            {"name": "availableBorrowsETH", "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv", "type": "uint256"},
            {"name": "healthFactor", "type": "uint256"},
        ],
    },
    {
        "name": "getReservesList",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]

ERC20_ABI: list = [
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
]


# --- Aave V2 event signatures -----------------------------------------------
#
# Lendle's proxy doesn't emit standard Aave V2 `Borrow` events — observed
# topic0s on Mantle as of 2026-05-18 are limited to: ReserveDataUpdated,
# Withdraw, Repay, LiquidationCall, FlashLoan, plus two custom topics.
#
# We discover borrowers via the events that *certainly* identify someone with
# outstanding debt:
#   - Repay        → user (topic[2]) repaid debt, so they must have borrowed
#   - LiquidationCall → user (topic[3]) had debt that triggered liquidation
# This is more robust than scanning Borrow events even on standard Aave V2,
# because it tracks the live debtor set rather than every historical opener.

# keccak256("Repay(address,address,address,uint256)")
REPAY_TOPIC = "0x4cdde6e09bb755c9a5589ebaec640bbfedff1362d4b255ebf8339782b9942faa"

# keccak256("LiquidationCall(address,address,address,uint256,uint256,address,bool)")
LIQUIDATION_TOPIC = (
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
)

# Tuple of (event_topic, topic_index_holding_user_address) used by discovery.
# topic[0] = event signature, topic[1..] are indexed parameters.
DISCOVERY_EVENTS = (
    (REPAY_TOPIC, 2),         # Repay: user is the 2nd indexed param → topic[2]
    (LIQUIDATION_TOPIC, 3),   # LiquidationCall: user is the 3rd indexed → topic[3]
)


# Aave V2 returns max uint256 for "infinite" HF (no debt). Match INIT's
# convention of using Decimal("999") so risk-level math behaves consistently.
_MAX_UINT256 = (1 << 256) - 1
_HF_SCALE = Decimal(10) ** 18


class LendleReader:
    """Reads Lendle (Aave V2 fork) positions on Mantle mainnet.

    Implements the `LendingProtocolReader` Protocol.
    """

    protocol_name: str = "lendle"

    def __init__(
        self,
        client: MantleClient,
        settings: Settings,
        *,
        discovery_block_window: int | None = None,
        max_log_chunk: int = 5_000,
        borrower_cache_path: Path | None = None,
    ):
        """
        discovery_block_window:
            How many blocks back to scan for Borrow events when refreshing the
            active-borrower set. 50k blocks ≈ 28 hours on Mantle (~2s/block).
            Larger = more borrowers tracked, slower discovery.

        max_log_chunk:
            Max range per eth_getLogs request. Public RPCs cap at ~10k blocks;
            5k leaves headroom.
        """
        self.client = client
        self.settings = settings
        self.discovery_block_window = (
            discovery_block_window
            if discovery_block_window is not None
            else settings.lendle_discovery_block_window
        )
        self.max_log_chunk = max_log_chunk

        self.lending_pool = client.contract(LENDLE_LENDING_POOL, LENDING_POOL_ABI)

        # Caches — reserve list rarely changes, symbol never does for a token.
        self._reserves: list[str] | None = None
        self._token_to_symbol: dict[str, str] = {}
        # Borrower set is cached across cycles. Persisted to disk so subsequent
        # restarts skip the (potentially expensive) deep discovery scan.
        # Tests pass a tmp_path here to keep their state hermetic.
        self._borrower_cache_path = borrower_cache_path or Path(".cache/lendle_borrowers.json")
        self._known_borrowers: set[str] = set()
        self._last_discovery_to_block: int | None = None
        self._load_borrower_cache()

    def _load_borrower_cache(self) -> None:
        if not self._borrower_cache_path.exists():
            return
        try:
            payload = json.loads(self._borrower_cache_path.read_text())
            self._known_borrowers = {
                Web3.to_checksum_address(a) for a in payload.get("borrowers", [])
            }
            self._last_discovery_to_block = payload.get("last_block")
            log.info(
                "lendle_borrower_cache_loaded",
                count=len(self._known_borrowers),
                last_block=self._last_discovery_to_block,
            )
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("lendle_borrower_cache_corrupt", err=str(e))

    def _save_borrower_cache(self) -> None:
        try:
            self._borrower_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._borrower_cache_path.write_text(json.dumps({
                "borrowers": sorted(self._known_borrowers),
                "last_block": self._last_discovery_to_block,
            }))
        except OSError as e:
            log.warning("lendle_borrower_cache_save_failed", err=str(e))

    # --- Reserves & symbols --------------------------------------------------

    def reserves(self) -> list[str]:
        if self._reserves is None:
            raw = self.lending_pool.functions.getReservesList().call()
            self._reserves = [Web3.to_checksum_address(a) for a in raw]
        return self._reserves

    def _symbol(self, token_addr: str) -> str | None:
        token_addr = Web3.to_checksum_address(token_addr)
        if token_addr in self._token_to_symbol:
            return self._token_to_symbol[token_addr] or None
        token = self.client.contract(token_addr, ERC20_ABI)
        try:
            sym = token.functions.symbol().call()
            self._token_to_symbol[token_addr] = sym
            return sym
        except (ContractLogicError, ValueError):
            self._token_to_symbol[token_addr] = ""
            return None

    # --- Borrower discovery --------------------------------------------------

    def discover_borrowers(self) -> set[str]:
        """Scan recent Borrow events, return the unique set of borrowers.

        Idempotent — re-discovering is fine; the set only grows. We track the
        last block scanned so subsequent cycles only scrape new blocks.
        """
        latest = self.client.w3.eth.block_number
        from_block = (
            self._last_discovery_to_block + 1
            if self._last_discovery_to_block is not None
            else max(0, latest - self.discovery_block_window)
        )

        if from_block >= latest:
            return self._known_borrowers

        log.info(
            "lendle_borrower_scan",
            from_block=from_block,
            to_block=latest,
            range=latest - from_block,
        )

        for chunk_start in range(from_block, latest + 1, self.max_log_chunk):
            chunk_end = min(chunk_start + self.max_log_chunk - 1, latest)
            for topic, user_idx in DISCOVERY_EVENTS:
                try:
                    logs = self.client.w3.eth.get_logs({
                        "fromBlock": chunk_start,
                        "toBlock": chunk_end,
                        "address": Web3.to_checksum_address(LENDLE_LENDING_POOL),
                        "topics": [topic],
                    })
                except Exception as e:  # noqa: BLE001
                    log.warning(
                        "lendle_log_fetch_failed",
                        from_block=chunk_start,
                        to_block=chunk_end,
                        topic=topic[:10],
                        err=str(e),
                    )
                    continue

                for entry in logs:
                    if len(entry["topics"]) > user_idx:
                        addr = "0x" + entry["topics"][user_idx].hex()[-40:]
                        if int(addr, 16) != 0:
                            self._known_borrowers.add(Web3.to_checksum_address(addr))

        self._last_discovery_to_block = latest
        log.info("lendle_borrowers_known", count=len(self._known_borrowers))
        self._save_borrower_cache()
        return self._known_borrowers

    # --- Account reads -------------------------------------------------------

    def get_user_account(self, user_addr: str) -> tuple[Decimal, Decimal, Decimal]:
        """Returns (collateral_usd, debt_usd, health_factor).

        HF is normalized: scaled out of 1e18, clamped to Decimal("999") when
        Aave reports max-uint256 (no debt sentinel).
        """
        try:
            (
                coll,
                debt,
                _avail,
                _liq_thr,
                _ltv,
                hf,
            ) = self.lending_pool.functions.getUserAccountData(
                Web3.to_checksum_address(user_addr)
            ).call()
        except (ContractLogicError, ValueError) as e:
            log.warning("lendle_user_read_failed", user=user_addr, err=str(e))
            return Decimal(0), Decimal(0), Decimal("999")

        if hf >= _MAX_UINT256 - 1 or debt == 0:
            hf_dec = Decimal("999")
        else:
            hf_dec = Decimal(hf) / _HF_SCALE

        # Lendle on Mantle uses USD as its reference currency, not ETH — the
        # docs confirm this. We pass the raw values through as USD with 18
        # decimals' worth of precision.
        coll_usd = Decimal(coll) / _HF_SCALE
        debt_usd = Decimal(debt) / _HF_SCALE
        return coll_usd, debt_usd, hf_dec

    # --- Top-level read path -------------------------------------------------

    def read_all_positions(self, max_positions: int | None = None) -> list[Position]:
        """Return active Lendle borrowers (debt > 0) with computed health.

        max_positions caps how many borrowers we read this cycle — useful on
        slow RPC. Borrowers are iterated in arbitrary set order; for a real
        deployment we'd prioritize by last-seen-block.
        """
        borrowers = self.discover_borrowers()
        candidates: Iterable[str] = borrowers
        if max_positions and max_positions > 0:
            candidates = list(borrowers)[:max_positions]

        positions: list[Position] = []
        skipped = 0
        for addr in candidates:
            coll, debt, hf = self.get_user_account(addr)
            if debt == 0:
                skipped += 1
                continue
            # Pick a "dominant collateral" symbol. Aave V2 aggregates USD across
            # all of a user's deposits — we don't get a per-asset breakdown from
            # getUserAccountData. We mark these UNKNOWN; a richer impl would
            # call ProtocolDataProvider.getUserReserveData per reserve.
            positions.append(Position(
                position_id=addr,           # Lendle is account-keyed, not NFT-keyed
                protocol="lendle",
                owner=addr,
                health_factor=hf,
                total_collateral_usd=coll,
                total_debt_usd=debt,
                dominant_collateral=None,   # see note above
            ))

        log.info(
            "lendle_positions_read",
            active=len(positions),
            skipped=skipped,
            known_borrowers=len(borrowers),
            at_risk=sum(1 for p in positions if p.risk_level in ("HIGH", "CRITICAL")),
        )
        return positions
