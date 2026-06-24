"""
Regression tests for M7 io_dm's cookie + proxy loaders.

Both functions touch Supabase, so we monkey-patch get_supabase to return a
canned row. The thing actually under test is the parsing/unwrapping logic.

Bug 2026-06-24: capture_session.py writes cookies as `{"cookies": [...]}`
but _load_cookies_for() used to return whatever JSON decoded to, including
the dict. Playwright's add_cookies() then raised "expected array, got
object" — silently broke every DM send.
"""
from __future__ import annotations
import json

import pytest
from cryptography.fernet import Fernet

from app.modules.m7_conversation import io_dm


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────


def _encrypt(payload) -> str:
    """Encrypt a JSON-serialisable payload the same way capture_session.py would."""
    fernet = Fernet(io_dm.FERNET_KEY.encode())
    return fernet.encrypt(json.dumps(payload).encode()).decode()


class _FakeSupabase:
    """Tiny stub that returns a canned row from a single 'select.eq.single.execute' chain."""

    def __init__(self, row: dict):
        self._row = row

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def single(self):
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = self._row
        return r


def _patch_supabase(monkeypatch, row: dict):
    monkeypatch.setattr(io_dm, "get_supabase", lambda: _FakeSupabase(row))


# ────────────────────────────────────────────────────────────────────
# _load_cookies_for — the bug we're fixing
# ────────────────────────────────────────────────────────────────────


SAMPLE_COOKIES = [
    {"name": "sessionid", "value": "abc", "domain": ".instagram.com", "path": "/"},
    {"name": "csrftoken", "value": "xyz", "domain": ".instagram.com", "path": "/"},
]


def test_load_cookies_unwraps_dict_shape(monkeypatch):
    """The shape capture_session.py actually writes: {'cookies': [...]}."""
    blob = _encrypt({"cookies": SAMPLE_COOKIES})
    _patch_supabase(monkeypatch, {"session_cookies_encrypted": blob})

    result = io_dm._load_cookies_for("ignorethisdump2")

    assert isinstance(result, list), "must return a list (Playwright requirement)"
    assert result == SAMPLE_COOKIES


def test_load_cookies_accepts_bare_list(monkeypatch):
    """Future-compat: a hand-seeded row stored as a bare list should also work."""
    blob = _encrypt(SAMPLE_COOKIES)
    _patch_supabase(monkeypatch, {"session_cookies_encrypted": blob})

    result = io_dm._load_cookies_for("any_account")

    assert result == SAMPLE_COOKIES


def test_load_cookies_raises_on_unknown_shape(monkeypatch):
    """Anything that isn't a list or {'cookies': list} should fail loudly."""
    blob = _encrypt({"not_cookies": SAMPLE_COOKIES})
    _patch_supabase(monkeypatch, {"session_cookies_encrypted": blob})

    with pytest.raises(RuntimeError, match="malformed"):
        io_dm._load_cookies_for("any_account")


def test_load_cookies_raises_when_blob_missing(monkeypatch):
    _patch_supabase(monkeypatch, {"session_cookies_encrypted": None})

    with pytest.raises(RuntimeError, match="no session cookies"):
        io_dm._load_cookies_for("any_account")


# ────────────────────────────────────────────────────────────────────
# _get_proxy_for — the bug we're also fixing
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("endpoint,expected", [
    ("",                          None),
    (None,                        None),
    ("direct://",                 None),
    ("http://u:p@h:1080",         {"server": "http://h:1080", "username": "u", "password": "p"}),
])
def test_get_proxy_for_handles_known_shapes(monkeypatch, endpoint, expected):
    """The duplicate parser previously here missed 'direct://' and sent it
    to Playwright as a literal proxy URL, causing ERR_NO_SUPPORTED_PROXIES."""
    _patch_supabase(monkeypatch, {"proxy_endpoint": endpoint})

    assert io_dm._get_proxy_for("any_account") == expected
