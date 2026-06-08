"""Common interface every lending-protocol reader implements.

Defining this as a `typing.Protocol` (structural typing — no inheritance
required) keeps each reader free of unnecessary base-class machinery. The
poller depends only on this signature; concrete readers (`InitCapitalReader`,
`LendleReader`) implement it independently.
"""

from __future__ import annotations

from typing import Protocol

from app.core.schema import Position


class LendingProtocolReader(Protocol):
    """Anything that can produce a list of Positions in one snapshot."""

    protocol_name: str
    """The Position.protocol slug emitted by this reader (e.g. 'init', 'lendle')."""

    def read_all_positions(self, max_positions: int | None = None) -> list[Position]:
        """Return active positions for one polling cycle.

        Implementations decide what 'active' means — INIT skips closed NFT
        positions, Lendle returns recent borrowers with non-zero debt.
        """
        ...
