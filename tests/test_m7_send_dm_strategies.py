"""
Tests for the new DM-compose strategy in M7 io_dm.

The function under test tries strategies in order. We can't run real
Playwright in unit tests but we can verify:
  - profile flow is tried FIRST
  - direct URL is tried only if profile fails
  - a terminal failure tries to screenshot before raising
  - the success path doesn't fall through to fallbacks
"""
from __future__ import annotations

import asyncio
import pytest
from cryptography.fernet import Fernet  # noqa: F401  (validates conftest's fernet key)

from app.modules.m7_conversation import io_dm


@pytest.fixture(autouse=True)
def _skip_humanize_sleep(monkeypatch):
    """Real _humanize_sleep does 1-3.5s asyncio.sleep — irrelevant for testing
    strategy ordering, and makes 4 tests take 17s instead of milliseconds."""
    async def _instant(*_a, **_kw):
        return None
    monkeypatch.setattr(io_dm, "_humanize_sleep", _instant)


# ────────────────────────────────────────────────────────────────────
# A minimal fake Page + Locator that records calls in order
# ────────────────────────────────────────────────────────────────────


class _FakeLocator:
    def __init__(self, behave: str = "ok"):
        self.behave = behave  # "ok" | "timeout" | "missing"

    @property
    def first(self):
        return self

    async def wait_for(self, **_kw):
        if self.behave == "timeout":
            raise io_dm.PWTimeoutError("locator wait_for timed out")

    async def click(self):
        if self.behave == "timeout":
            raise io_dm.PWTimeoutError("locator click timed out")

    async def count(self):
        return 1 if self.behave == "ok" else 0

    async def is_visible(self):
        return self.behave == "ok"


class _FakePage:
    """Records every interaction so tests can assert strategy ordering."""

    def __init__(
        self,
        *,
        profile_works: bool,
        direct_url_works: bool,
        continue_gate_on_first_visit: bool = False,
    ):
        self.profile_works = profile_works
        self.direct_url_works = direct_url_works
        # If True, the first goto() to any URL lands on the IG "Continue as"
        # picker. Subsequent gotos to the same URL succeed normally.
        self.continue_gate_on_first_visit = continue_gate_on_first_visit
        self._visited: set[str] = set()
        self.navigations: list[str] = []
        self.selectors_waited: list[str] = []
        self.screenshots: list[str] = []
        self.continue_clicks: int = 0
        self.url = "about:blank"
        # _gate_present is True when the IG picker overlay should be detected.
        # Initially matches continue_gate_on_first_visit; gets flipped to
        # False after a Continue button click.
        self._gate_present = continue_gate_on_first_visit

    async def goto(self, url: str, **_kw):
        self.navigations.append(url)
        # Simulate the IG Continue-gate redirect: first visit to a URL gets
        # bounced to /accounts/login/, second visit serves the real page.
        if self.continue_gate_on_first_visit and url not in self._visited:
            self._visited.add(url)
            self._gate_present = True
            self.url = (
                "https://www.instagram.com/accounts/login/?next="
                + url.replace("https://", "")
            )
        else:
            self._visited.add(url)
            self.url = url

    async def wait_for_load_state(self, _state: str, **_kw):
        return None

    async def wait_for_function(self, _fn, **_kw):
        # Real Playwright polls the JS function until it returns truthy.
        # In tests, gate dismissal flips _gate_present to False — that's
        # all the function would observe. Succeed immediately if the gate
        # is gone; otherwise raise TimeoutError.
        if not self._gate_present:
            return None
        raise io_dm.PWTimeoutError("wait_for_function: gate still present")

    async def wait_for_selector(self, selector: str, **_kw):
        self.selectors_waited.append(selector)
        if selector == io_dm.SELECTORS["profile_message_button"]:
            if not self.profile_works:
                raise io_dm.PWTimeoutError("profile button not found")
        elif selector == io_dm.SELECTORS["message_input"]:
            last_nav = self.navigations[-1] if self.navigations else ""
            if "/direct/t/" in last_nav:
                if not self.direct_url_works:
                    raise io_dm.PWTimeoutError("direct URL: input not visible")
            else:
                if not self.profile_works:
                    raise io_dm.PWTimeoutError("profile: input not visible")

    def locator(self, selector: str):
        # The Continue gate's button selectors include "Continue".
        # Detect any selector that mentions Continue and return a clicker
        # that counts how many times Continue was actually pressed.
        if "Continue" in selector:
            return _ContinueButton(self, present=self._gate_present)
        if selector == io_dm.SELECTORS["profile_message_button"]:
            return _FakeLocator("ok" if self.profile_works else "timeout")
        return _FakeLocator("ok")

    async def screenshot(self, path: str, **_kw):
        self.screenshots.append(path)


class _ContinueButton:
    """Behaves like a Playwright locator for the 'Continue as <user>' button.

    Visible only when a gate is being detected. Clicking it bumps a counter
    (so tests can assert the gate was dismissed) and "navigates" away by
    flipping the page state so subsequent visibility checks return False.
    """

    def __init__(self, page: "_FakePage", *, present: bool):
        self._page = page
        self._present = present
        # Once clicked, future is_visible() calls return False — matches the
        # real picker's behavior, which disappears after click.
        self._was_clicked = False

    @property
    def first(self):
        return self

    async def count(self):
        if self._was_clicked:
            return 0
        return 1 if self._present else 0

    async def is_visible(self):
        if self._was_clicked:
            return False
        return self._present

    async def click(self, **_kw):
        # Real signature accepts force=True, delay=80, etc. — accept any kwargs.
        if not self._present:
            return
        self._page.continue_clicks += 1
        self._was_clicked = True
        # After the click, the gate is dismissed. Update the page URL away
        # from /accounts/login/ so subsequent checks pass.
        self._page.url = "https://www.instagram.com/"
        # Also tell the page that the gate is gone — used by wait_for_function.
        self._page._gate_present = False


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


def test_profile_strategy_tried_first(monkeypatch, tmp_path):
    """When profile strategy works, direct URL is never attempted."""
    monkeypatch.setattr(io_dm, "DEBUG_DIR", tmp_path)
    page = _FakePage(profile_works=True, direct_url_works=False)

    async def _run():
        await io_dm._open_dm_compose(page, "vendiction_")
    asyncio.run(_run())

    # Only one navigation — to the profile.
    assert page.navigations == ["https://www.instagram.com/vendiction_/"]
    # No screenshot taken on success.
    assert page.screenshots == []


def test_falls_back_to_direct_url_when_profile_fails(monkeypatch, tmp_path):
    """If profile-button strategy fails, the direct URL is the next attempt."""
    monkeypatch.setattr(io_dm, "DEBUG_DIR", tmp_path)
    page = _FakePage(profile_works=False, direct_url_works=True)

    async def _run():
        await io_dm._open_dm_compose(page, "vendiction_")
    asyncio.run(_run())

    assert page.navigations == [
        "https://www.instagram.com/vendiction_/",
        "https://www.instagram.com/direct/t/vendiction_/",
    ]
    assert page.screenshots == []  # success — no screenshot


def test_terminal_failure_screenshots_and_raises(monkeypatch, tmp_path):
    """Both strategies fail → screenshot to disk + RuntimeError."""
    monkeypatch.setattr(io_dm, "DEBUG_DIR", tmp_path)
    page = _FakePage(profile_works=False, direct_url_works=False)

    async def _run():
        await io_dm._open_dm_compose(page, "vendiction_")

    with pytest.raises(RuntimeError, match="DM compose unreachable"):
        asyncio.run(_run())

    # Tried both URLs.
    assert page.navigations == [
        "https://www.instagram.com/vendiction_/",
        "https://www.instagram.com/direct/t/vendiction_/",
    ]
    # Captured exactly one screenshot in the debug dir.
    assert len(page.screenshots) == 1
    assert str(tmp_path) in page.screenshots[0]
    assert "vendiction_" in page.screenshots[0]
    assert page.screenshots[0].endswith(".png")


def test_screenshot_failure_does_not_mask_main_error(monkeypatch, tmp_path):
    """If the screenshot itself fails, we still raise the compose-unreachable error."""
    monkeypatch.setattr(io_dm, "DEBUG_DIR", tmp_path)
    page = _FakePage(profile_works=False, direct_url_works=False)

    async def _broken_screenshot(*_a, **_kw):
        raise OSError("disk full")

    page.screenshot = _broken_screenshot  # type: ignore[assignment]

    async def _run():
        await io_dm._open_dm_compose(page, "vendiction_")

    # The screenshot error is logged but swallowed — the real failure is raised.
    with pytest.raises(RuntimeError, match="DM compose unreachable"):
        asyncio.run(_run())


# ────────────────────────────────────────────────────────────────────
# Continue-gate dismissal — discovered live via failure screenshot
# ────────────────────────────────────────────────────────────────────


def test_continue_gate_clicked_and_navigation_retried(monkeypatch, tmp_path):
    """When IG redirects to the 'Continue as <user>' picker, we click
    Continue and re-navigate to the target URL."""
    monkeypatch.setattr(io_dm, "DEBUG_DIR", tmp_path)
    page = _FakePage(
        profile_works=True,
        direct_url_works=False,
        continue_gate_on_first_visit=True,
    )

    async def _run():
        await io_dm._open_dm_compose(page, "vendiction_")
    asyncio.run(_run())

    # Continue was clicked exactly once on the profile strategy.
    assert page.continue_clicks == 1
    # The profile URL was visited twice: first hit landed on the gate,
    # second hit after dismissing should reach the actual profile.
    assert page.navigations.count("https://www.instagram.com/vendiction_/") == 2


def test_dismiss_continue_gate_returns_false_when_not_on_login(monkeypatch):
    """The gate-dismiss helper should be a no-op outside /accounts/login/."""
    page = _FakePage(profile_works=True, direct_url_works=True)
    page.url = "https://www.instagram.com/vendiction_/"

    async def _run():
        return await io_dm._dismiss_continue_gate(page)

    result = asyncio.run(_run())
    assert result is False
    assert page.continue_clicks == 0


def test_dismiss_continue_gate_handles_login_url_with_no_button(monkeypatch):
    """If we're on /accounts/login/ but no Continue button is visible
    (e.g. real challenge screen, not the picker), return False cleanly."""
    page = _FakePage(profile_works=False, direct_url_works=False)
    # Force login URL but make _ContinueButton report not-present by
    # claiming we already visited (so the gate flag doesn't trigger),
    # then manually set login URL.
    page.url = "https://www.instagram.com/accounts/login/?next=/something/"
    # Override the locator so Continue button is invisible.
    def _no_continue(selector: str):
        if "Continue" in selector:
            return _ContinueButton(page, present=False)
        return _FakeLocator("ok")
    page.locator = _no_continue  # type: ignore[assignment]

    async def _run():
        return await io_dm._dismiss_continue_gate(page)

    result = asyncio.run(_run())
    assert result is False
    assert page.continue_clicks == 0
