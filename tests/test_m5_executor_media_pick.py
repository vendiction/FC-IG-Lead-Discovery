"""Tests for M5 executor's media-index picker (pure, no IG)."""
from __future__ import annotations

import pytest

from app.modules.m5_warmup.executor import pick_media_index


def test_first_like_picks_first_media():
    """No prior likes, 3 available media → index 0."""
    assert pick_media_index(prior_executed_likes=0, available=3) == 0


def test_second_like_picks_second_media():
    """1 prior like, 3 available → index 1 (no longer always 0 — the bug)."""
    assert pick_media_index(prior_executed_likes=1, available=3) == 1


def test_third_like_picks_third_media():
    assert pick_media_index(prior_executed_likes=2, available=3) == 2


def test_fourth_like_wraps_to_first():
    """Round-robin: more prior likes than available media → wrap."""
    assert pick_media_index(prior_executed_likes=3, available=3) == 0
    assert pick_media_index(prior_executed_likes=4, available=3) == 1


def test_single_media_always_picks_zero():
    """If the prospect has only one recent post, that's the only target."""
    for prior in range(0, 5):
        assert pick_media_index(prior_executed_likes=prior, available=1) == 0


def test_no_media_available_raises():
    """fetch returned zero — caller's job to handle, but the picker raises."""
    with pytest.raises(ValueError, match="no media available"):
        pick_media_index(prior_executed_likes=0, available=0)


def test_two_consecutive_likes_target_different_media():
    """
    The actual regression: V1 always returned 0, so two scheduled likes
    on the same prospect both targeted the same media — second was a
    silent no-op against an already-liked post. This is what the planner
    schedules (2 likes per prospect) and what must produce distinct
    targets.
    """
    media_count = 3
    first  = pick_media_index(prior_executed_likes=0, available=media_count)
    second = pick_media_index(prior_executed_likes=1, available=media_count)
    assert first != second, "two consecutive likes must hit different media"
