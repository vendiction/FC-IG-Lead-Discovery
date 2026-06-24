"""Tests for operator-mode behavior.

In operator mode (the default as of 2026-06-24), every IG-write action
routes through Discord instead of Playwright. These tests lock in:

  - M5 planner schedules every action with status='skipped_human_queue'
  - Per-action human_payload builders return the expected shape
  - decide_story_actions still works (probabilistic story scheduling
    operates the same way; only the execution route changed)

The Discord poller/embed-builder behavior is exercised by the integration
shape (the bot can boot, the queries are well-formed) — full Discord
round-trip is covered by live testing, not unit tests, since it requires
a Discord channel + bot token to assert anything meaningful.
"""
from __future__ import annotations
import random

from app.modules.m5_warmup.planner import (
    _build_follow_payload,
    _build_like_payload,
    _build_story_payload,
    decide_story_actions,
)


# ────────────────────────────────────────────────────────────────────
# Payload shape — what the Discord card renders from
# ────────────────────────────────────────────────────────────────────

def test_follow_payload_carries_context():
    prospect = {
        "handle": "healthcoachclaudia",
        "score": 74,
        "follower_count": 31000,
        "primary_gap": "lead_magnet_missing",
        "cross_platform_discovery_source": "tiktok",
    }
    payload = _build_follow_payload(prospect)
    assert payload["handle"] == "healthcoachclaudia"
    assert payload["score"] == 74
    assert payload["follower_count"] == 31000
    assert payload["primary_gap"] == "lead_magnet_missing"
    assert payload["cross_platform_discovery_source"] == "tiktok"
    assert "follow" in payload["instructions"].lower()


def test_follow_payload_tolerates_missing_fields():
    """Real prospects may not have all the optional fields populated.
    The payload builder should not crash on sparse input."""
    payload = _build_follow_payload({"handle": "minimal_handle"})
    assert payload["handle"] == "minimal_handle"
    # None values are fine; the embed renderer handles them.
    assert payload["score"] is None
    assert payload["follower_count"] is None


def test_like_payload_includes_like_number():
    prospect = {"handle": "h"}
    p1 = _build_like_payload(prospect, like_number=1)
    p2 = _build_like_payload(prospect, like_number=2)
    assert p1["like_number"] == 1
    assert p2["like_number"] == 2
    # Instructions should reference the specific like number so the
    # operator doesn't get confused which post to like.
    assert "#1" in p1["instructions"]
    assert "#2" in p2["instructions"]


def test_story_payload_view_vs_like():
    """The instructions should differ between view (swipe) and like (heart)."""
    prospect = {"handle": "h"}
    view = _build_story_payload(prospect, action="view")
    like = _build_story_payload(prospect, action="like")
    assert view["action_type"] == "view"
    assert like["action_type"] == "like"
    assert "swipe" in view["instructions"].lower()
    assert "heart" in like["instructions"].lower()


def test_story_payload_mentions_skip_command():
    """Stories expire — the operator needs to know they can skip cleanly."""
    payload = _build_story_payload({"handle": "h"}, action="view")
    assert "/story_skip" in payload["instructions"]


# ────────────────────────────────────────────────────────────────────
# Probability logic — unchanged by operator mode but worth re-asserting
# ────────────────────────────────────────────────────────────────────

def test_story_actions_still_probabilistic_after_operator_mode():
    """decide_story_actions is a pure function — operator mode shouldn't
    have touched it. Re-verify the (True, False), (True, True), and
    (False, False) outcomes are still all reachable."""
    seen_states = set()
    rng = random.Random(42)
    for _ in range(500):
        view, like = decide_story_actions(rng)
        seen_states.add((view, like))
    # Across 500 trials we should hit at least (True, False), (True, True),
    # and (False, False). The (False, True) outcome is unreachable by design.
    assert (True, False) in seen_states
    assert (True, True) in seen_states
    assert (False, False) in seen_states
    assert (False, True) not in seen_states


# ────────────────────────────────────────────────────────────────────
# Planner schedule — every action goes to operator queue
# ────────────────────────────────────────────────────────────────────

def test_planner_constant_is_operator_status():
    """Lock in the contract that planner uses 'skipped_human_queue' for
    every action in operator mode. If someone reverts the planner to
    'scheduled' for any non-comment action, this test catches it."""
    # We don't import OPERATOR_STATUS directly because it's a local var in
    # plan_for_prospect — but we can read the source and assert the string
    # appears in the action-row construction. Robust enough for V1.
    import inspect
    from app.modules.m5_warmup import planner

    src = inspect.getsource(planner.plan_for_prospect)
    # The follow / like / story / comment row dicts should ALL set status
    # to skipped_human_queue (not 'scheduled') in operator mode.
    assert 'OPERATOR_STATUS = "skipped_human_queue"' in src

    # Count action-row insertions — they should all reference OPERATOR_STATUS,
    # not the legacy 'scheduled' literal.
    scheduled_literals = src.count('"status": "scheduled"')
    assert scheduled_literals == 0, (
        f"plan_for_prospect should not schedule any action with "
        f"status='scheduled' in operator mode (found {scheduled_literals})"
    )
