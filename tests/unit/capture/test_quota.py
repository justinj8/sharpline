from sharpline.capture.quota import QuotaGuard
from sharpline.sources.fetched import QuotaInfo


def test_guard_refuses_over_cap() -> None:
    guard = QuotaGuard(session_cap=10, floor=0)
    guard.charge(None, 8)
    decision = guard.allows(3)
    assert not decision.allowed
    assert decision.reason is not None and "session_cap" in decision.reason


def test_guard_allows_exactly_at_cap() -> None:
    guard = QuotaGuard(session_cap=10, floor=0)
    guard.charge(None, 7)
    decision = guard.allows(3)
    assert decision.allowed and decision.reason is None


def test_guard_respects_floor() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.charge(QuotaInfo(last=1, used=478, remaining=22), 1)
    decision = guard.allows(6)
    assert not decision.allowed
    assert decision.reason is not None and "floor" in decision.reason
    assert guard.allows(2).allowed  # 22 - 2 == 20 >= floor


def test_guard_remaining_unknown_skips_floor_check() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    assert guard.remaining is None
    assert guard.allows(6).allowed


def test_guard_charges_actual_cost() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.charge(QuotaInfo(last=4, used=100, remaining=400), 6)
    assert guard.credits_used == 4
    assert guard.remaining == 400


def test_guard_charges_estimate_when_actual_missing() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.charge(None, 6)
    assert guard.credits_used == 6
    guard.charge(QuotaInfo(last=None, used=None, remaining=None), 3)
    assert guard.credits_used == 9
    assert guard.remaining is None


def test_guard_keeps_last_known_remaining() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.charge(QuotaInfo(last=3, used=10, remaining=490), 3)
    guard.charge(QuotaInfo(last=3, used=13, remaining=None), 3)
    assert guard.remaining == 490
    assert guard.credits_used == 6


def test_guard_reserve_counts_against_cap() -> None:
    guard = QuotaGuard(session_cap=20, floor=0)
    guard.charge(None, 5)
    assert guard.allows(6, reserve=9).allowed  # 5 + 6 + 9 == 20
    decision = guard.allows(7, reserve=9)
    assert not decision.allowed
    assert decision.reason is not None and "reserve 9" in decision.reason


def test_guard_reserve_counts_against_floor() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.charge(QuotaInfo(last=0, used=0, remaining=35), 0)
    assert guard.allows(6).allowed
    decision = guard.allows(6, reserve=10)  # 35 - 6 - 10 < 20
    assert not decision.allowed
    assert decision.reason is not None and decision.reason.startswith("floor")
    assert guard.floor == 20 and guard.session_cap == 180


def test_seed_counts_the_slate_days_earlier_credits_against_the_cap() -> None:
    """F5: the cap is per slate day: credits earlier runs spent that day are already used."""
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.seed(used=170, remaining=None)
    assert guard.credits_used == 170 and guard.remaining is None
    assert guard.allows(10).allowed
    assert not guard.allows(11).allowed


def test_seed_starts_from_the_last_known_remaining() -> None:
    guard = QuotaGuard(session_cap=180, floor=20)
    guard.seed(used=0, remaining=26)
    assert guard.remaining == 26
    assert guard.allows(6).allowed and not guard.allows(7).allowed
    guard.charge(QuotaInfo(last=3, used=477, remaining=23), 3)
    assert (guard.credits_used, guard.remaining) == (3, 23)  # provider headers win from here
