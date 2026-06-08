"""INIT Capital position reader.

Verified against live Mantle mainnet as of 2026-05-16:
  - PosManager (0x0e740...A92) exposes totalSupply/tokenByIndex/ownerOf
    (ERC-721 Enumerable) plus getPosCollInfo, getPosBorrInfo, getPosMode.
  - InitCore (0x972Bc...Fc5) exposes getCollateralCreditCurrent_e36 /
    getBorrowCreditCurrent_e36 — aggregate USD credit in 1e36 fixed point.
  - InitOracle (0x4E195...350) exposes getPrice_e36(token) for USD pricing.
  - LendingPool (each pool, ERC-20-ish) exposes asset(), toAmt(shares),
    decimals(), symbol() — used to map LP shares back to underlying.

INIT has 332k+ historical positions; most are closed (no debt). The reader
skips empty positions to keep poll cycles cheap.

References:
  https://dev.init.capital/contract-addresses/mantle
  https://docs.init.capital/borrowing/health-factor
"""

from __future__ import annotations

from decimal import Decimal

from web3 import Web3
from web3.exceptions import ContractLogicError

from app.core.config import Settings
from app.core.logging_setup import get_logger
from app.core.schema import CollateralLeg, DebtLeg, Position
from app.services.mantle_client import MantleClient

log = get_logger(__name__)


# --- ABIs --------------------------------------------------------------------
# Minimal — only the functions we actually call.

POS_MANAGER_ABI: list = [
    {
        "name": "totalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "tokenByIndex",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "index", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "ownerOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "getPosCollInfo",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "posId", "type": "uint256"}],
        "outputs": [
            {"name": "pools", "type": "address[]"},
            {"name": "amts", "type": "uint256[]"},
        ],
    },
    {
        "name": "getPosBorrInfo",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "posId", "type": "uint256"}],
        "outputs": [
            {"name": "pools", "type": "address[]"},
            {"name": "shares", "type": "uint256[]"},
        ],
    },
    {
        "name": "getPosMode",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "posId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint16"}],
    },
]

INIT_CORE_ABI: list = [
    {
        "name": "getCollateralCreditCurrent_e36",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "posId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "getBorrowCreditCurrent_e36",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "posId", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

LENDING_POOL_ABI: list = [
    {
        "name": "asset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "underlyingToken",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "toAmt",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "debtShareToAmtCurrent",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ERC20_ABI: list = [
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
]

# INIT scales USD credits by 1e36 to keep precision on-chain.
E36 = Decimal(10) ** 36


class InitCapitalReader:
    """Reads INIT Capital positions and computes health factors.

    Implements the `LendingProtocolReader` Protocol.
    """

    protocol_name: str = "init"

    def __init__(self, client: MantleClient, settings: Settings):
        self.client = client
        self.settings = settings
        addrs = settings.init_addresses
        self.pos_manager = client.contract(addrs.pos_manager, POS_MANAGER_ABI)
        self.init_core = client.contract(addrs.init_core, INIT_CORE_ABI)

        # Per-pool caches — pool address → underlying token address & symbol.
        # Pool deployments are immutable, so a one-shot lookup per pool is fine.
        self._pool_to_underlying: dict[str, str] = {}
        self._token_to_symbol: dict[str, str] = {}

    # --- Position enumeration ---

    def total_positions(self) -> int:
        return int(self.pos_manager.functions.totalSupply().call())

    def position_id_at(self, index: int) -> int:
        return int(self.pos_manager.functions.tokenByIndex(index).call())

    def owner_of(self, position_id: int) -> str:
        return self.pos_manager.functions.ownerOf(position_id).call()

    def get_mode(self, position_id: int) -> int | None:
        try:
            return int(self.pos_manager.functions.getPosMode(position_id).call())
        except (ContractLogicError, ValueError):
            return None

    # --- Health math ---

    def credits_e36(self, position_id: int) -> tuple[Decimal, Decimal]:
        """Returns (collateral_credit_usd, borrow_credit_usd).

        The nonpayable annotation lets INIT accrue interest in a real tx;
        eth_call simulates without writes — same numbers, no gas.
        """
        try:
            coll_raw = self.init_core.functions.getCollateralCreditCurrent_e36(position_id).call()
            borr_raw = self.init_core.functions.getBorrowCreditCurrent_e36(position_id).call()
        except ContractLogicError as e:
            log.warning("credits_read_failed", position_id=str(position_id), err=str(e))
            return Decimal(0), Decimal(0)
        return Decimal(coll_raw) / E36, Decimal(borr_raw) / E36

    # --- Per-leg discovery ---

    def coll_info(self, position_id: int) -> tuple[list[str], list[int]]:
        """Per-position collateral legs: parallel arrays of (pool, lp_shares)."""
        try:
            pools, amts = self.pos_manager.functions.getPosCollInfo(position_id).call()
        except (ContractLogicError, ValueError):
            return [], []
        return list(pools), list(amts)

    def borr_info(self, position_id: int) -> tuple[list[str], list[int]]:
        """Per-position debt legs: parallel arrays of (pool, debt_shares)."""
        try:
            pools, shares = self.pos_manager.functions.getPosBorrInfo(position_id).call()
        except (ContractLogicError, ValueError):
            return [], []
        return list(pools), list(shares)

    def _pool_underlying(self, pool_addr: str) -> str | None:
        """Resolve a lending-pool address to its underlying ERC-20 (cached).

        Different INIT pool versions name the accessor `asset()` or
        `underlyingToken()`; we try both before giving up.
        """
        pool_addr = Web3.to_checksum_address(pool_addr)
        if pool_addr in self._pool_to_underlying:
            return self._pool_to_underlying[pool_addr]

        pool = self.client.contract(pool_addr, LENDING_POOL_ABI)
        for fn_name in ("underlyingToken", "asset"):
            try:
                underlying = pool.functions[fn_name]().call()
                if underlying and int(underlying, 16) != 0:
                    underlying = Web3.to_checksum_address(underlying)
                    self._pool_to_underlying[pool_addr] = underlying
                    return underlying
            except (ContractLogicError, ValueError, KeyError):
                continue

        log.warning("pool_underlying_lookup_failed", pool=pool_addr)
        self._pool_to_underlying[pool_addr] = ""  # negative-cache
        return None

    def _token_symbol(self, token_addr: str) -> str | None:
        """Resolve an ERC-20 to its symbol (cached)."""
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

    def _build_legs(
        self, pools: list[str], amounts: list[int], *, is_debt: bool
    ) -> list[CollateralLeg | DebtLeg]:
        """Build CollateralLeg/DebtLeg list from raw pool+amount arrays."""
        legs: list[CollateralLeg | DebtLeg] = []
        for pool, amt in zip(pools, amounts):
            pool_cs = Web3.to_checksum_address(pool)
            underlying = self._pool_underlying(pool_cs) or pool_cs
            symbol = self._token_symbol(underlying) if underlying else None
            if is_debt:
                legs.append(DebtLeg(
                    pool=pool_cs,
                    token=underlying,
                    symbol=symbol,
                    amount_shares=Decimal(amt),
                ))
            else:
                legs.append(CollateralLeg(
                    pool=pool_cs,
                    token=underlying,
                    symbol=symbol,
                    amount_shares=Decimal(amt),
                ))
        return legs

    # --- Top-level read path ---

    def read_position(self, position_id: int, *, skip_inactive: bool = True) -> Position | None:
        """Read one position's full state.

        Returns None for inactive positions (no debt) when skip_inactive=True.
        Inactive positions can't trigger cascades; skipping them keeps poll
        cycles cheap on a 332k-position protocol.
        """
        # Cheap check first: positions with no debt can't liquidate.
        if skip_inactive:
            borr_pools, borr_shares = self.borr_info(position_id)
            if not borr_pools:
                return None
        else:
            borr_pools, borr_shares = self.borr_info(position_id)

        coll_usd, debt_usd = self.credits_e36(position_id)
        if skip_inactive and debt_usd == 0:
            return None

        try:
            owner = Web3.to_checksum_address(self.owner_of(position_id))
        except (ContractLogicError, ValueError) as e:
            log.warning("owner_read_failed", position_id=str(position_id), err=str(e))
            return None

        coll_pools, coll_amts = self.coll_info(position_id)
        coll_legs = self._build_legs(coll_pools, coll_amts, is_debt=False)
        debt_legs = self._build_legs(borr_pools, borr_shares, is_debt=True)

        health = coll_usd / debt_usd if debt_usd > 0 else Decimal("999")

        # Pick a dominant collateral asset for cascade clustering. We use the
        # first leg's symbol because INIT positions almost always have a single
        # dominant collateral type; refining to "max by USD value" requires
        # per-leg pricing (Phase 1.3 follow-up).
        dominant = coll_legs[0].symbol if coll_legs else None

        return Position(
            position_id=str(position_id),
            protocol="init",
            owner=owner,
            mode=self.get_mode(position_id),
            collateral=coll_legs,  # type: ignore[arg-type]
            debt=debt_legs,        # type: ignore[arg-type]
            health_factor=health,
            total_collateral_usd=coll_usd,
            total_debt_usd=debt_usd,
            dominant_collateral=dominant,
        )

    def read_all_positions(self, max_positions: int | None = None) -> list[Position]:
        """Enumerate positions, filtering out closed/empty ones.

        Iterates from the highest indices first — newer positions are more
        likely active. The `max_positions` cap counts indices scanned, not
        active positions returned.
        """
        total = self.total_positions()
        scan_limit = min(total, max_positions) if max_positions and max_positions > 0 else total

        log.info("reading_positions", total_supply=total, scan_limit=scan_limit)
        positions: list[Position] = []
        skipped = 0
        progress_every = max(scan_limit // 10, 10)

        # Scan newest first
        for offset in range(scan_limit):
            i = total - 1 - offset
            try:
                pid = self.position_id_at(i)
            except (ContractLogicError, ValueError) as e:
                log.warning("position_index_failed", index=i, err=str(e))
                continue

            pos = self.read_position(pid, skip_inactive=True)
            if pos is None:
                skipped += 1
            else:
                positions.append(pos)

            scanned = offset + 1
            if scanned % progress_every == 0 or scanned == scan_limit:
                log.info(
                    "reading_progress",
                    scanned=scanned,
                    total=scan_limit,
                    active=len(positions),
                    skipped=skipped,
                )

        log.info(
            "positions_read",
            active=len(positions),
            skipped_inactive=skipped,
            at_risk=sum(1 for p in positions if p.risk_level in ("HIGH", "CRITICAL")),
        )
        return positions
