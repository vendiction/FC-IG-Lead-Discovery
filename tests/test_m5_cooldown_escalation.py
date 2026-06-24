"""Tests for M5's escalating soft-block cooldown.

When IG soft-blocks the same burner twice in a short window, the system
should park the account much longer instead of re-attempting every 2h
and digging the reputation hole deeper.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.m5_warmup.executor import (
    COOLDOWN_HOURS_ON_REPEAT_BLOCK,
    COOLDOWN_HOURS_ON_SOFT_BLOCK,
    RECENT_BLOCK_WINDOW_HOURS,
    decide_cooldown_hours,
)


NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_first_block_uses_short_cooldown():
    """No prior soft-block recorded → treat as a one-off."""
    assert decide_cooldown_hours(None, NOW) == COOLDOWN_HOURS_ON_SOFT_BLOCK


def test_recent_repeat_uses_long_cooldown():
    """A second block within 24h means IG has the account flagged.
    Park it for a full day instead of bashing the rate limiter."""
    one_hour_ago = NOW - timedelta(hours=1)
    assert decide_cooldown_hours(one_hour_ago, NOW) == COOLDOWN_HOURS_ON_REPEAT_BLOCK


def test_block_at_exactly_window_boundary_still_long():
    """Edge case: a block exactly 23h59m ago is still within window."""
    almost_a_day = NOW - timedelta(hours=RECENT_BLOCK_WINDOW_HOURS - 0.01)
    assert decide_cooldown_hours(almost_a_day, NOW) == COOLDOWN_HOURS_ON_REPEAT_BLOCK


def test_old_block_resets_to_short_cooldown():
    """A block from days ago is no longer 'recent' — back to short cooldown."""
    days_ago = NOW - timedelta(days=3)
    assert decide_cooldown_hours(days_ago, NOW) == COOLDOWN_HOURS_ON_SOFT_BLOCK


def test_block_exactly_at_window_boundary_is_short():
    """Right at the 24h boundary — outside the recent window."""
    exact = NOW - timedelta(hours=RECENT_BLOCK_WINDOW_HOURS)
    assert decide_cooldown_hours(exact, NOW) == COOLDOWN_HOURS_ON_SOFT_BLOCK


def test_repeat_long_cooldown_is_meaningfully_longer():
    """The long cooldown should be at least 4× the short one to actually
    change IG's behavior — anything less is just whack-a-mole."""
    assert COOLDOWN_HOURS_ON_REPEAT_BLOCK >= COOLDOWN_HOURS_ON_SOFT_BLOCK * 4
