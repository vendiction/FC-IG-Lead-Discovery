"""Tests for M5 planner's story-action additions.

Mason's spec line:
    "heart story updates and perform occasional swiping interactions
     to create a history of engagement"

decide_story_actions() is the pure decision; tests pin a Random seed so
the probabilistic logic is deterministic per case.
"""
from __future__ import annotations
import random

import pytest

from app.modules.m5_warmup.planner import (
    STORY_LIKE_GIVEN_VIEW_PROBABILITY,
    STORY_VIEW_PROBABILITY,
    decide_story_actions,
)


def test_decide_story_actions_returns_two_bools():
    """Sanity: the function returns a (bool, bool) tuple."""
    rng = random.Random(0)
    view, like = decide_story_actions(rng)
    assert isinstance(view, bool)
    assert isinstance(like, bool)


def test_like_implies_view():
    """You cannot like a story you didn't view — the planner shouldn't be
    able to schedule a like without a view. Across 200 trials, every time
    like is True, view must also be True."""
    rng = random.Random(42)
    for _ in range(200):
        view, like = decide_story_actions(rng)
        if like:
            assert view, "story_like scheduled without story_view — invariant broken"


def test_no_view_means_no_like():
    """When the first roll fails, the function shouldn't even roll for like."""
    # Force the first random() to return well above STORY_VIEW_PROBABILITY.
    class _Rng:
        def __init__(self):
            self.calls = 0
        def random(self):
            self.calls += 1
            return 0.99  # always above threshold

    rng = _Rng()
    view, like = decide_story_actions(rng)
    assert view is False
    assert like is False
    # And only ONE random() call happened — short-circuited, no like roll.
    assert rng.calls == 1


def test_view_only_when_first_roll_passes_but_second_fails():
    """Test the (True, False) outcome explicitly."""
    rolls = iter([0.0, 0.99])  # first roll passes, second fails

    class _Rng:
        def random(self):
            return next(rolls)

    view, like = decide_story_actions(_Rng())
    assert view is True
    assert like is False


def test_view_and_like_when_both_rolls_pass():
    """Test the (True, True) outcome explicitly."""
    rolls = iter([0.0, 0.0])  # both rolls pass

    class _Rng:
        def random(self):
            return next(rolls)

    view, like = decide_story_actions(_Rng())
    assert view is True
    assert like is True


def test_probability_constants_are_sane():
    """Sanity bounds — values must be in [0, 1] for the logic to behave."""
    assert 0.0 <= STORY_VIEW_PROBABILITY <= 1.0
    assert 0.0 <= STORY_LIKE_GIVEN_VIEW_PROBABILITY <= 1.0


def test_distribution_approximates_target(monkeypatch):
    """Over 10000 trials with seeded random, the observed view-rate and
    like-rate should be within 3% of the configured probabilities.

    This catches a class of bug where someone changes the probability
    constant but the logic ignores it (e.g. hardcoded compare value).
    """
    rng = random.Random(123)
    n = 10_000
    views = 0
    likes = 0
    for _ in range(n):
        v, l = decide_story_actions(rng)
        if v:
            views += 1
        if l:
            likes += 1

    observed_view_rate = views / n
    expected_view_rate = STORY_VIEW_PROBABILITY
    assert abs(observed_view_rate - expected_view_rate) < 0.03, (
        f"observed view rate {observed_view_rate:.3f} too far from "
        f"target {expected_view_rate:.3f}"
    )

    # Likes are conditional on views passing.
    expected_like_rate = (
        STORY_VIEW_PROBABILITY * STORY_LIKE_GIVEN_VIEW_PROBABILITY
    )
    observed_like_rate = likes / n
    assert abs(observed_like_rate - expected_like_rate) < 0.03, (
        f"observed like rate {observed_like_rate:.3f} too far from "
        f"target {expected_like_rate:.3f}"
    )
