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
    def session_cap(self) -> int:
        return self._session_cap

    @property
    def credits_used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int | None:
        """Latest provider-reported remaining credits; None until known."""
        return self._remaining

    @property
    def floor(self) -> int:
        return self._floor

    def allows(self, estimated_cost: int, reserve: int = 0) -> Decision:
        """Allow a call costing `estimated_cost` while keeping `reserve` credits spare.

        The runner reserves the estimates of pending closings for non-closing calls, so
        earlier refreshes can never spend the credits a closing snapshot needs.
        """
        plus = f" + reserve {reserve}" if reserve else ""
        minus = f" - reserve {reserve}" if reserve else ""
        if self._used + estimated_cost + reserve > self._session_cap:
            return Decision(
                False,
                f"session_cap: used {self._used} + est {estimated_cost}{plus}"
                f" > cap {self._session_cap}",
            )
        if self._remaining is not None and self._remaining - estimated_cost - reserve < self._floor:
            return Decision(
                False,
                f"floor: remaining {self._remaining} - est {estimated_cost}{minus}"
                f" < floor {self._floor}",
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
