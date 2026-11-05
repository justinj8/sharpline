"""Runtime credit guard for metered provider calls (spec §4.3)."""

from dataclasses import dataclass

from sharpline.sources.fetched import QuotaInfo


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


class QuotaGuard:
    """Allows a metered call only inside the session cap and above the provider floor."""

    def __init__(self, session_cap: int, floor: int) -> None:
        self._session_cap = session_cap
        self._floor = floor
        self._used = 0
        self._remaining: int | None = None

    @property
    def credits_used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int | None:
        """Latest provider-reported remaining credits; None until known."""
        return self._remaining

    def allows(self, estimated_cost: int) -> Decision:
        if self._used + estimated_cost > self._session_cap:
            return Decision(
                False,
                f"session_cap: used {self._used} + est {estimated_cost} > cap {self._session_cap}",
            )
        if self._remaining is not None and self._remaining - estimated_cost < self._floor:
            return Decision(
                False,
                f"floor: remaining {self._remaining} - est {estimated_cost} < floor {self._floor}",
            )
        return Decision(True)

    def charge(self, actual: QuotaInfo | None, estimated_cost: int) -> None:
        """Record a call's cost: the provider's `last` when reported, else the estimate."""
        if actual is not None and actual.last is not None:
            self._used += actual.last
        else:
            self._used += estimated_cost
        if actual is not None and actual.remaining is not None:
            self._remaining = actual.remaining
