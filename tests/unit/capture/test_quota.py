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
