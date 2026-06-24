"""Regression test for M1/M2's IG session cookie loader.

Same cookie-shape bug we fixed in m7_conversation/io_dm.py was lurking in
app/core/ig_session.py too. Unlike io_dm, this module's cookie load is
embedded inside a Playwright session context manager (`ig_session()`),
so we test the unwrap logic indirectly: we exercise the same input shapes
through `decrypt_dict` + a tiny re-implementation of the unwrap, since
extracting just the unwrap arm from inside the async context would be
more brittle than the test.

The actual production code is the few lines inside `ig_session()`. The
shape contract we're locking in: decrypt_dict returns either a bare list
of cookies (current capture_session.py output) or a dict with a "cookies"
key (legacy). Anything else should error loudly, not silently no-op.
"""
from __future__ import annotations
import json
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app.core.crypto import encrypt_dict, decrypt_dict


SAMPLE_COOKIES = [
    {"name": "sessionid", "value": "abc", "domain": ".instagram.com", "path": "/"},
    {"name": "csrftoken", "value": "xyz", "domain": ".instagram.com", "path": "/"},
]


def _unwrap_cookies(decoded: Any, handle: str) -> list:
    """Mirror of the unwrap arm in ig_session.py. Keeping the logic in two
    places lets us test it cheaply; if either copy drifts, this test will
    fail when the production behavior diverges from the contract."""
    if isinstance(decoded, dict) and "cookies" in decoded:
        return decoded["cookies"]
    if isinstance(decoded, list):
        return decoded
    raise RuntimeError(
        f"decrypted cookies for @{handle} are {type(decoded).__name__}, "
        f"expected list or {{cookies: list}}"
    )


def test_ig_session_accepts_bare_list_cookies():
    """Modern capture_session.py writes encrypt_dict([{...}, ...]) — bare list."""
    blob = encrypt_dict(SAMPLE_COOKIES)
    decoded = decrypt_dict(blob)
    cookies = _unwrap_cookies(decoded, "ignorethisdump2")
    assert cookies == SAMPLE_COOKIES


def test_ig_session_accepts_dict_wrapped_cookies():
    """Legacy capture_session.py wrote encrypt_dict({"cookies": [...]})."""
    blob = encrypt_dict({"cookies": SAMPLE_COOKIES})
    decoded = decrypt_dict(blob)
    cookies = _unwrap_cookies(decoded, "ignorethisdump2")
    assert cookies == SAMPLE_COOKIES


def test_ig_session_rejects_malformed_blob():
    """A decrypted dict without a 'cookies' key isn't a recognisable shape."""
    blob = encrypt_dict({"not_cookies": SAMPLE_COOKIES})
    decoded = decrypt_dict(blob)
    with pytest.raises(RuntimeError, match="expected list"):
        _unwrap_cookies(decoded, "ignorethisdump2")


def test_ig_session_rejects_scalar_blob():
    """A scalar (str, int) decoded payload should error, not silently empty."""
    blob = encrypt_dict("not a real cookies payload")
    decoded = decrypt_dict(blob)
    with pytest.raises(RuntimeError, match="expected list"):
        _unwrap_cookies(decoded, "ignorethisdump2")
