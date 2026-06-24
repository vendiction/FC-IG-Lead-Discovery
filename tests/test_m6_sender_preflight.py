"""Tests for M6 opener sender preflight logic (pure, no I/O)."""
from __future__ import annotations
import os
import pytest

# Ensure the cap default applies in tests
os.environ.pop("OPENER_SEND_MAX_FOLLOWER_COUNT", None)

from app.modules.m6_opener import sender
from app.modules.m6_opener.sender import preflight_check, PreflightFail


@pytest.fixture(autouse=True)
def _allow_dm_cap(monkeypatch):
    """Real can_send_dm_today queries Supabase and returns False under the
    stubbed client. These tests are about the OTHER preflight rules; force
    the cap to be permissive so failures are about what we're actually
    testing. Tests that specifically check the cap can override this."""
    monkeypatch.setattr(sender, "can_send_dm_today", lambda _h: (True, 0, 25))


# ── Pass case ───────────────────────────────────────────────────────

def test_preflight_passes_for_in_range_prospect():
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "Saw you on TikTok, weird question...",
    }
    prospect = {"follower_count": 80_000, "handle": "somecoach"}
    # Should NOT raise
    preflight_check(opener, prospect, ig_account="my_burner")


# ── Failure modes ───────────────────────────────────────────────────

def test_preflight_blocks_unapproved():
    opener = {
        "approved_for_send": False,
        "sent_at": None,
        "opener_text": "hi",
    }
    with pytest.raises(PreflightFail, match="not approved"):
        preflight_check(opener, {"follower_count": 5_000, "handle": "x"}, ig_account="x")


def test_preflight_blocks_already_sent():
    opener = {
        "approved_for_send": True,
        "sent_at": "2026-06-23T01:00:00Z",
        "opener_text": "hi",
    }
    with pytest.raises(PreflightFail, match="already sent"):
        preflight_check(opener, {"follower_count": 5_000, "handle": "x"}, ig_account="x")


def test_preflight_blocks_empty_text():
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "",
    }
    with pytest.raises(PreflightFail, match="empty"):
        preflight_check(opener, {"follower_count": 5_000, "handle": "x"}, ig_account="x")


def test_preflight_blocks_celebrities():
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "Saw you on TikTok...",
    }
    # hormozi tier
    prospect = {"follower_count": 5_400_000, "handle": "hormozi"}
    with pytest.raises(PreflightFail, match="celebrity tier"):
        preflight_check(opener, prospect, ig_account="x")


# ── Primary signal: is_celebrity_disqualified flag from M4 ──────────


def test_preflight_blocks_when_is_celebrity_disqualified_flag_true():
    """
    Realistic production path: qualified_prospects has no follower_count column,
    so the dict from conv_repo.get_qualified_prospect() carries
    is_celebrity_disqualified=True (set by M4) instead.
    """
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "Saw you on TikTok...",
    }
    prospect = {
        "handle": "hormozi",
        "is_celebrity_disqualified": True,
        "celebrity_dq_reason": "follower_count 5400000 exceeds FOLLOWER_HARD_DQ_CAP (500000)",
        # NOTE: no follower_count key — this is the real shape from the DB
    }
    with pytest.raises(PreflightFail, match="celebrity tier"):
        preflight_check(opener, prospect, ig_account="x")


def test_preflight_celebrity_reason_propagates_into_failure_message():
    opener = {"approved_for_send": True, "sent_at": None, "opener_text": "hi"}
    prospect = {
        "handle": "x",
        "is_celebrity_disqualified": True,
        "celebrity_dq_reason": "follower_count 5400000 exceeds cap",
    }
    with pytest.raises(PreflightFail) as exc_info:
        preflight_check(opener, prospect, ig_account="x")
    assert "5400000" in str(exc_info.value)


def test_preflight_passes_when_celeb_flag_false_and_no_follower_count():
    """
    The bug we're fixing: previously this case silently passed because
    follower_count defaulted to 0. Still passes — but only because the
    primary signal explicitly says is_celebrity_disqualified=False.
    """
    opener = {"approved_for_send": True, "sent_at": None, "opener_text": "hi"}
    prospect = {
        "handle": "somecoach",
        "is_celebrity_disqualified": False,
        # no follower_count, no celebrity_dq_reason — clean prospect
    }
    # Should NOT raise
    preflight_check(opener, prospect, ig_account="my_burner")


def test_preflight_passes_when_celeb_flag_absent_and_no_follower_count():
    """
    Defensive case: dict doesn't carry the flag at all (older row pre-M4
    schema change). Pass — no positive signal of celeb status anywhere.
    Documented behavior, not a happy outcome.
    """
    opener = {"approved_for_send": True, "sent_at": None, "opener_text": "hi"}
    prospect = {"handle": "somecoach"}
    preflight_check(opener, prospect, ig_account="my_burner")


def test_preflight_passes_right_at_cap():
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "hi",
    }
    # default cap is 500_000 — exactly at cap should pass (the test is >)
    prospect = {"follower_count": 500_000, "handle": "x"}
    preflight_check(opener, prospect, ig_account="x")


def test_preflight_blocks_one_over_cap():
    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "hi",
    }
    prospect = {"follower_count": 500_001, "handle": "x"}
    with pytest.raises(PreflightFail, match="celebrity tier"):
        preflight_check(opener, prospect, ig_account="x")


# ── Cap is env-tunable ──────────────────────────────────────────────

def test_preflight_respects_env_cap(monkeypatch):
    """Override the cap via env — verify both directions."""
    import importlib
    monkeypatch.setenv("OPENER_SEND_MAX_FOLLOWER_COUNT", "10000")
    from app.modules.m6_opener import sender as _sender
    importlib.reload(_sender)

    # importlib.reload re-binds the module's references, undoing the
    # autouse fixture's monkeypatch. Re-apply it on the reloaded module.
    monkeypatch.setattr(_sender, "can_send_dm_today", lambda _h: (True, 0, 25))

    opener = {
        "approved_for_send": True,
        "sent_at": None,
        "opener_text": "hi",
    }
    # 20K should now fail (was passing under default 500K)
    with pytest.raises(_sender.PreflightFail, match="celebrity tier"):
        _sender.preflight_check(opener, {"follower_count": 20_000, "handle": "x"}, "x")

    # 5K should still pass
    _sender.preflight_check(opener, {"follower_count": 5_000, "handle": "x"}, "x")
