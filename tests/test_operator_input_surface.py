"""Tests for the Discord input-surface normalize helpers.

These two pure functions handle the messiness of how operators paste
handles and hashtags — sometimes with @, sometimes with #, sometimes
as URLs, sometimes with trailing slashes or query strings.
"""
from __future__ import annotations

import pytest

from app.core.input_normalize import (
    normalize_handle as _normalize_handle,
    normalize_hashtag as _normalize_hashtag,
)


# ────────────────────────────────────────────────────────────────────
# _normalize_handle
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("healthcoachclaudia", "healthcoachclaudia"),
    ("@healthcoachclaudia", "healthcoachclaudia"),
    ("  HealthCoachClaudia  ", "healthcoachclaudia"),
    ("HEALTHCOACHCLAUDIA", "healthcoachclaudia"),
    ("https://instagram.com/healthcoachclaudia", "healthcoachclaudia"),
    ("https://instagram.com/healthcoachclaudia/", "healthcoachclaudia"),
    ("https://www.instagram.com/healthcoachclaudia/?hl=en", "healthcoachclaudia"),
    ("instagram.com/healthcoachclaudia", "healthcoachclaudia"),
])
def test_normalize_handle_strips_decoration(raw, expected):
    assert _normalize_handle(raw) == expected


def test_normalize_handle_handles_dots_and_underscores():
    # IG handles can contain dots and underscores — we shouldn't strip them
    assert _normalize_handle("@vendiction_") == "vendiction_"
    assert _normalize_handle("@health.coach.claudia") == "health.coach.claudia"


# ────────────────────────────────────────────────────────────────────
# _normalize_hashtag
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("learndropshipping", "learndropshipping"),
    ("#learndropshipping", "learndropshipping"),
    ("  LearnDropshipping  ", "learndropshipping"),
    ("##doubletag", "doubletag"),     # lstrip strips all leading #s
    ("HEALTHCOACHLIFE", "healthcoachlife"),
])
def test_normalize_hashtag_strips_decoration(raw, expected):
    assert _normalize_hashtag(raw) == expected
