"""Tests for the cooldown reaper's pure decision logic (no DB)."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest

from app.core.cooldown_reaper import CooldownRow, decide_reapable_accounts


NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def _row(handle, status, cooldown_until):
    return CooldownRow(handle=handle, current_status=status, cooldown_until=cooldown_until)


def test_empty_input_returns_empty():
    assert decide_reapable_accounts([], now=NOW) == []


def test_active_account_never_reaped():
    rows = [_row("burner1", "active", NOW - timedelta(hours=10))]
    assert decide_reapable_accounts(rows, now=NOW) == []


def test_expired_cooldown_is_reaped():
    rows = [_row("burner1", "cooldown", NOW - timedelta(minutes=1))]
    assert decide_reapable_accounts(rows, now=NOW) == ["burner1"]


def test_future_cooldown_is_not_reaped():
    rows = [_row("burner1", "cooldown", NOW + timedelta(hours=1))]
    assert decide_reapable_accounts(rows, now=NOW) == []


def test_indefinite_cooldown_never_reaped():
    """cooldown_until=None means 'manual hold' — only a human clears it."""
    rows = [_row("burner1", "cooldown", None)]
    assert decide_reapable_accounts(rows, now=NOW) == []


def test_exactly_at_expiry_is_reaped():
    """cooldown_until == now is the threshold — should reap (<=, not <)."""
    rows = [_row("burner1", "cooldown", NOW)]
    assert decide_reapable_accounts(rows, now=NOW) == ["burner1"]


def test_mixed_input_picks_only_expired_cooldowns():
    rows = [
        _row("active_acct", "active", None),
        _row("future_cd", "cooldown", NOW + timedelta(hours=1)),
        _row("expired_cd", "cooldown", NOW - timedelta(minutes=1)),
        _row("indefinite_cd", "cooldown", None),
        _row("disabled_acct", "disabled", NOW - timedelta(hours=99)),
        _row("another_expired", "cooldown", NOW - timedelta(seconds=1)),
    ]
    result = decide_reapable_accounts(rows, now=NOW)
    assert sorted(result) == ["another_expired", "expired_cd"]


def test_banned_account_with_old_cooldown_not_reaped():
    """A banned account that happens to have a past cooldown_until stays banned."""
    rows = [_row("burner1", "banned", NOW - timedelta(hours=99))]
    assert decide_reapable_accounts(rows, now=NOW) == []


def test_warming_status_not_reaped():
    """current_status='warming' (not 'cooldown') is left alone."""
    rows = [_row("burner1", "warming", NOW - timedelta(hours=99))]
    assert decide_reapable_accounts(rows, now=NOW) == []
