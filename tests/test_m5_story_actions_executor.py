"""Tests for M5 executor's story-action additions.

Covers:
  - _pick_story selects a story from the available list
  - NoActiveStories is a real exception class the executor can catch
  - The exception inherits from Exception (not BaseException) so a
    `except Exception` block doesn't unintentionally swallow it before
    we get to our purpose-built handler
"""
from __future__ import annotations

import pytest

from app.modules.m5_warmup.executor import NoActiveStories, _pick_story


def test_pick_story_returns_one_of_provided():
    """Whatever is in the list, the picker returns one of those exact dicts."""
    stories = [
        {"id": "1_x", "pk": "1", "taken_at": 100, "owner_pk": "x"},
        {"id": "2_x", "pk": "2", "taken_at": 200, "owner_pk": "x"},
        {"id": "3_x", "pk": "3", "taken_at": 300, "owner_pk": "x"},
    ]
    picked = _pick_story(stories)
    assert picked in stories


def test_pick_story_single_item_always_returns_it():
    stories = [{"id": "only_x", "pk": "only", "taken_at": 100, "owner_pk": "x"}]
    assert _pick_story(stories) is stories[0]


def test_pick_story_distribution_over_many_calls():
    """With 3 stories and 1000 picks, each should appear at least once.

    Random.choice is uniform, so seeing zero hits on any element across
    1000 trials would indicate something is very wrong with the picker.
    """
    stories = [
        {"id": "a", "pk": "a", "taken_at": 100, "owner_pk": "x"},
        {"id": "b", "pk": "b", "taken_at": 200, "owner_pk": "x"},
        {"id": "c", "pk": "c", "taken_at": 300, "owner_pk": "x"},
    ]
    seen = set()
    for _ in range(1000):
        seen.add(_pick_story(stories)["id"])
    assert seen == {"a", "b", "c"}


def test_no_active_stories_is_exception():
    """NoActiveStories must be raisable + catchable, and inherit from
    Exception (not BaseException) so blanket `except Exception` traps catch
    it correctly when the executor wants them to."""
    assert issubclass(NoActiveStories, Exception)
    with pytest.raises(NoActiveStories, match="something"):
        raise NoActiveStories("something")
