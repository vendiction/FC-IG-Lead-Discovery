"""
Shared test fixtures and module stubs.

Pytest loads this file before any test module, so the stubs we inject into
sys.modules here win against the real imports. That lets unit tests run in
sandboxes that don't have the `supabase` PyPI package, Playwright browsers,
or production env vars on hand.

Stubs are intentionally permissive — they expose every attribute the real
modules expose that any test reaches for, so individual test files don't
need to install their own (which used to cause inter-test pollution where
one test's narrow stub broke another test's import).

Tests that need to touch real Supabase / Playwright should be marked
`@pytest.mark.integration` and skipped in CI without env vars.
"""
from __future__ import annotations
import os
import sys
import types


# ────────────────────────────────────────────────────────────────────
# Required env vars — Settings() refuses to instantiate without them
# ────────────────────────────────────────────────────────────────────

_TEST_ENV_DEFAULTS = {
    "SUPABASE_URL": "https://test.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "test_service_role_key",
    "SUPABASE_ANON_KEY": "test_anon_key",
    "ANTHROPIC_API_KEY": "test_anthropic_key",
    # Real, valid Fernet key — required because some tests actually encrypt
    # payloads to test the decrypt path (e.g. tests/test_m7_io_dm_loaders.py).
    # Generated via Fernet.generate_key(); not a secret, only used in tests.
    "FERNET_KEY": "BZkdnNAECNVAUQ506W8ZH2gHs7cYGYrH3OygfWkSHMI=",
}
for key, value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(key, value)


# ────────────────────────────────────────────────────────────────────
# Stub: supabase
# ────────────────────────────────────────────────────────────────────

if "supabase" not in sys.modules:
    _supabase_mod = types.ModuleType("supabase")

    class _Client:  # pragma: no cover — stub
        def table(self, _name):
            return self

        def select(self, *_a, **_k): return self
        def eq(self, *_a, **_k): return self
        def neq(self, *_a, **_k): return self
        def lt(self, *_a, **_k): return self
        def lte(self, *_a, **_k): return self
        def gt(self, *_a, **_k): return self
        def gte(self, *_a, **_k): return self
        def is_(self, *_a, **_k): return self
        def in_(self, *_a, **_k): return self
        def or_(self, *_a, **_k): return self
        def order(self, *_a, **_k): return self
        def limit(self, *_a, **_k): return self
        def single(self, *_a, **_k): return self
        def insert(self, *_a, **_k): return self
        def update(self, *_a, **_k): return self
        def delete(self, *_a, **_k): return self

        @property
        def not_(self):
            return self

        def execute(self):
            class _R:
                data = []
                count = 0
            return _R()

    def _create_client(_url, _key):  # pragma: no cover — stub
        return _Client()

    _supabase_mod.Client = _Client  # type: ignore[attr-defined]
    _supabase_mod.create_client = _create_client  # type: ignore[attr-defined]
    sys.modules["supabase"] = _supabase_mod


# ────────────────────────────────────────────────────────────────────
# Stub: playwright.async_api
# Comprehensive — exposes everything any module under test imports.
# ────────────────────────────────────────────────────────────────────

if "playwright.async_api" not in sys.modules:
    _pw_mod = types.ModuleType("playwright.async_api")

    class _PWStub:  # pragma: no cover — stub
        pass

    async def _pw_async(*_a, **_k):  # pragma: no cover — stub
        return None

    class _PWTimeoutError(Exception):  # pragma: no cover — stub
        pass

    _pw_mod.async_playwright = _pw_async  # type: ignore[attr-defined]
    _pw_mod.Browser = _PWStub  # type: ignore[attr-defined]
    _pw_mod.BrowserContext = _PWStub  # type: ignore[attr-defined]
    _pw_mod.Page = _PWStub  # type: ignore[attr-defined]
    _pw_mod.TimeoutError = _PWTimeoutError  # type: ignore[attr-defined]
    sys.modules["playwright.async_api"] = _pw_mod


# ────────────────────────────────────────────────────────────────────
# Note on real modules:
# We do NOT stub app.core.logging, app.core.ig_session, app.core.rate_limiter,
# app.modules.m1_tagged_crawler.ig_api, or app.modules.m5_warmup.ig_actions.
# With Supabase and Playwright stubbed above, the real modules import
# cleanly and offer the right attributes (get_logger, _parse_proxy, etc.).
# Individual tests should NOT replace these in sys.modules — that's what
# used to cause "fine in isolation, broken in suite" failures.
# ────────────────────────────────────────────────────────────────────
