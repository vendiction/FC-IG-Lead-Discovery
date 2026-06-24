"""
M7 — Instagram DM I/O via Playwright.

Two public functions:
- poll_inbox(ig_account_handle) → list of (prospect_handle, new_messages)
- send_dm(ig_account_handle, prospect_handle, text) → ig_message_id (or None)

Session management:
- Each IG account has Fernet-encrypted cookies in ig_accounts.session_cookies_encrypted
- We decrypt + inject before each navigation
- Persistent context per IG account stored at /app/ig_sessions/<handle>/

Rate limiting:
- Caller (worker.py) is responsible for checking ig_account_usage.dms_sent
  against ig_accounts.daily_caps['dms_sent'] BEFORE calling send_dm.
- This module does NOT enforce caps — separation of concerns.

⚠️ DOM SELECTORS: Instagram's DOM changes frequently. The selectors below
   are best-effort against the current web UI as of build time. If poll
   or send fails consistently, run scripts/inspect_ig_dm.py to refresh
   selectors and update this file.
"""
from __future__ import annotations
import asyncio
import json
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import structlog
from cryptography.fernet import Fernet
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
)

from app.core.supabase_client import get_supabase  # type: ignore

log = structlog.get_logger("m7.io_dm")


# ── Config ──────────────────────────────────────────────────────────

SESSION_DIR = Path(os.getenv("IG_SESSION_DIR", "/app/ig_sessions"))
DEBUG_DIR = Path(os.getenv("IG_DEBUG_DIR", "/app/ig_sessions/_debug"))
FERNET_KEY = os.environ.get("FERNET_KEY", "")  # set at deploy time

# Human-like pacing — random sleep ranges in seconds
PACING = {
    "between_actions_min": 1.2,
    "between_actions_max": 3.5,
    "typing_char_min": 0.04,
    "typing_char_max": 0.12,
    "before_send_min": 0.8,
    "before_send_max": 2.0,
}

# Selectors (best-effort against current IG DOM)
SELECTORS = {
    "inbox_thread_list": "div[role='listbox'] a[href*='/direct/t/']",
    "thread_unread_dot": "div[aria-label='Unread']",
    "thread_message_row": "div[role='row']",
    "thread_message_text": "div[dir='auto']",
    "message_input": "div[contenteditable='true'][role='textbox']",
    "send_button": "div[role='button']:has-text('Send')",
    "thread_header_username": "header h2",
    # Profile → Message button. IG renders Message as either a real <button>
    # (logged-in own profile) or a div with role='button'. We accept either.
    "profile_message_button": (
        "div[role='button']:has-text('Message'), "
        "a[href^='/direct/t/']:has-text('Message'), "
        "button:has-text('Message')"
    ),
    # Message-request modal that sometimes appears when DMing someone who doesn't follow you.
    "request_confirm_send": "div[role='button']:has-text('Send')",
}


# ────────────────────────────────────────────────────────────────────
# Session cookie helpers
# ────────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    if not FERNET_KEY:
        raise RuntimeError("FERNET_KEY not set — cannot decrypt IG session cookies")
    return Fernet(FERNET_KEY.encode())


def _load_cookies_for(handle: str) -> list[dict]:
    """Pull encrypted cookies from ig_accounts table, decrypt, return cookie list.

    Handles both shapes seen in the wild:
    - {"cookies": [...]}   — what scripts/capture_session.py writes today
    - [...]                — bare list, future-compat / hand-seeded rows

    Bug fix 2026-06-24: previously returned whatever JSON decoded to, which
    meant Playwright's add_cookies() got the dict and raised
    "expected array, got object". The unwrap below is idempotent on the
    raw-list shape.
    """
    sb = get_supabase()
    r = (sb.table("ig_accounts").select("session_cookies_encrypted")
         .eq("handle", handle).single().execute())
    blob = r.data.get("session_cookies_encrypted")
    if not blob:
        raise RuntimeError(f"no session cookies stored for IG account @{handle}")
    raw = _fernet().decrypt(blob.encode()).decode()
    decoded = json.loads(raw)

    # Unwrap the {"cookies": [...]} shape if present
    if isinstance(decoded, dict) and "cookies" in decoded:
        cookies = decoded["cookies"]
    else:
        cookies = decoded

    if not isinstance(cookies, list):
        raise RuntimeError(
            f"decrypted cookies for @{handle} are {type(cookies).__name__}, "
            f"expected list — stored blob is malformed"
        )
    return cookies


def _get_proxy_for(handle: str) -> Optional[dict]:
    """Read proxy_endpoint and return a Playwright proxy dict (or None).

    Delegates to app.core.ig_session._parse_proxy so M5 and M7 agree on what
    'no proxy' means. The duplicate inline parser previously here only
    recognized ("", "none", "direct") case-sensitively and missed the
    "direct://" scheme that capture_session.py writes — leading to
    ERR_NO_SUPPORTED_PROXIES when Playwright tried to use it literally.
    """
    from app.core.ig_session import _parse_proxy  # local import avoids cycles
    sb = get_supabase()
    r = (sb.table("ig_accounts").select("proxy_endpoint")
         .eq("handle", handle).single().execute())
    endpoint = r.data.get("proxy_endpoint") or ""
    return _parse_proxy(endpoint)


# ────────────────────────────────────────────────────────────────────
# Browser lifecycle (one persistent context per IG account)
# ────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def _ig_session(handle: str):
    """Yield a (browser, context, page) tuple wired up for the given IG account."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir = str(SESSION_DIR / handle)
    proxy = _get_proxy_for(handle)

    async with async_playwright() as pw:
        # Use persistent context — keeps Chromium profile (storage, cache) per account
        context: BrowserContext = await pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=os.getenv("M7_HEADLESS", "true").lower() == "true",
            proxy=proxy,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Manila",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
        )

        # Inject cookies if persistent context doesn't already have them
        try:
            cookies = _load_cookies_for(handle)
            await context.add_cookies(cookies)
        except Exception as e:
            log.warning("m7.io.cookies_inject_failed", handle=handle, err=str(e))

        page = await context.new_page()
        try:
            yield (context, page)
        finally:
            await context.close()


async def _humanize_sleep(min_s: float, max_s: float) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _type_humanlike(page: Page, selector: str, text: str) -> None:
    """Type one char at a time with random delay."""
    locator = page.locator(selector).first
    await locator.click()
    for ch in text:
        await locator.press(ch if len(ch) == 1 and ch.isprintable() else "")
        await asyncio.sleep(random.uniform(PACING["typing_char_min"], PACING["typing_char_max"]))


# ────────────────────────────────────────────────────────────────────
# Public API: poll_inbox
# ────────────────────────────────────────────────────────────────────

async def poll_inbox(handle: str) -> list[dict]:
    """
    Scan inbox for the given IG account, return list of dicts:
        [{prospect_handle: str, messages: [{body, ig_message_id, received_at}]}]

    Only returns threads with UNREAD messages — read threads are skipped.
    Caller is responsible for matching prospect_handle → conversation_id.
    """
    log.info("m7.io.poll.start", handle=handle)
    out: list[dict] = []

    async with _ig_session(handle) as (_ctx, page):
        try:
            await page.goto("https://www.instagram.com/direct/inbox/", timeout=20_000)
            await page.wait_for_selector(SELECTORS["inbox_thread_list"], timeout=15_000)
        except PWTimeoutError:
            log.error("m7.io.poll.inbox_load_failed", handle=handle)
            return out

        await _humanize_sleep(PACING["between_actions_min"], PACING["between_actions_max"])

        # Find unread threads — IG marks them with a dot or distinct styling
        thread_links = await page.locator(SELECTORS["inbox_thread_list"]).all()
        log.info("m7.io.poll.threads_seen", handle=handle, count=len(thread_links))

        for i, link in enumerate(thread_links):
            # Check if this thread has the unread dot
            has_unread = await link.locator(SELECTORS["thread_unread_dot"]).count() > 0
            if not has_unread:
                continue

            # Get prospect handle from thread header
            try:
                await link.click()
                await page.wait_for_selector(SELECTORS["thread_header_username"], timeout=10_000)
                prospect_handle = (
                    await page.locator(SELECTORS["thread_header_username"]).first.inner_text()
                ).strip().lstrip("@")
            except PWTimeoutError:
                log.warning("m7.io.poll.thread_open_failed", handle=handle, thread_index=i)
                continue

            # Pull all message rows currently rendered; filter to inbound (from prospect)
            msg_rows = await page.locator(SELECTORS["thread_message_row"]).all()
            messages: list[dict] = []
            for row in msg_rows[-15:]:  # last ~15 messages is enough; we dedupe via DB
                # Heuristic: a message is inbound if it's left-aligned (no 'justify-content: flex-end')
                # The robust check would need to inspect computed style; for now collect all
                # text and let the caller dedupe by content + DB lookup.
                text_el = row.locator(SELECTORS["thread_message_text"]).first
                try:
                    body = (await text_el.inner_text(timeout=2_000)).strip()
                except PWTimeoutError:
                    continue
                if not body:
                    continue
                messages.append({
                    "body": body,
                    "ig_message_id": None,   # IG web doesn't expose stable IDs; dedupe via body+ts
                    "received_at": datetime.now(timezone.utc).isoformat(),
                })

            if messages:
                out.append({
                    "prospect_handle": prospect_handle,
                    "messages": messages,
                })
                log.info(
                    "m7.io.poll.unread_found",
                    handle=handle,
                    prospect=prospect_handle,
                    msg_count=len(messages),
                )

            await _humanize_sleep(PACING["between_actions_min"], PACING["between_actions_max"])

    log.info("m7.io.poll.done", handle=handle, prospects_with_new=len(out))
    return out


# ────────────────────────────────────────────────────────────────────
# Public API: send_dm
# ────────────────────────────────────────────────────────────────────

async def _dismiss_continue_gate(page: "Page") -> bool:
    """If IG is showing its "Continue as <user>" account picker, click the
    real inner button (the one with aria-label="Continue <username>") and
    wait for the auth handshake to settle.

    Returns True if a gate was actually dismissed.

    DOM forensics (2026-06-24 inspect_continue_gate.py run):
    The picker renders as nested divs all carrying "Continue" in their text.
    Clicking the wrapper (`div[role='button']:has-text('Continue')`) hit
    the outer wrapper [11] and did nothing — the actual click handler is
    on the inner element [13]:
        <div aria-label="Continue ignorethisdump2"
             role="button" tabindex="0">

    The aria-label always starts with "Continue " followed by the username,
    so we match `[aria-label^='Continue ']` to ignore the wrapper layers
    that share text but not the aria-label.

    Also: the picker can appear as a side panel on `/` (home feed) OR on
    `/accounts/login/`. We detect by selector presence, not by URL.
    """
    # Selectors ordered most-specific first. Aria-label match is the win.
    continue_selectors = [
        "div[aria-label^='Continue '][role='button']",
        "button[aria-label^='Continue ']",
        # Fallbacks for variants that don't carry the username in aria-label
        "button[type='submit']:has-text('Continue')",
        "button:has-text('Continue')",
    ]
    for sel in continue_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible():
                log.info("m7.io.send.continue_gate_detected", selector=sel)
                # IG's picker uses React event handlers — a plain .click()
                # sometimes fires the visual button but doesn't dispatch the
                # React synthetic click. force=True bypasses Playwright's
                # actionability check and sends a real mouse event.
                await btn.click(force=True, delay=80)
                # Wait for either a URL change (real navigation) or the
                # picker disappearing (modal-style dismissal).
                try:
                    await page.wait_for_function(
                        """sel => !document.querySelector(sel)?.checkVisibility?.()""",
                        arg=sel,
                        timeout=8_000,
                    )
                except PWTimeoutError:
                    # No visibility change — the picker is still up, click
                    # didn't take. Try the next selector.
                    log.warning(
                        "m7.io.send.continue_gate_click_ineffective",
                        selector=sel,
                    )
                    continue
                await _humanize_sleep(
                    PACING["between_actions_min"], PACING["between_actions_max"]
                )
                log.info("m7.io.send.continue_gate_dismissed", url=page.url)
                return True
        except Exception as e:
            log.debug(
                "m7.io.send.continue_gate_selector_skipped",
                selector=sel,
                err=str(e),
            )
    return False


async def _open_dm_compose(page: "Page", prospect_handle: str) -> None:
    """
    Open a DM compose box with the recipient ready to type.

    Tries strategies in order, raises RuntimeError if all fail.

    Strategy 1: profile → Message button.
        Most reliable. Works whether or not a prior thread exists.
        Reliable across IG UI versions because the Message button is the
        canonical entry point for "send a DM" from a logged-in browser.

    Strategy 2: direct thread URL `/direct/t/{handle}/`.
        Sometimes works as an alias on certain IG versions. The previous
        implementation relied on this alone; it timed out 21 s in production
        because the URL resolves to inbox on most accounts (no compose box).

    Strategy 3: bail.
        On terminal failure we screenshot what IG actually rendered so the
        next debug session has visual evidence — selectors break silently
        otherwise.

    Each strategy also dismisses the "Continue as <user>" login gate if IG
    redirected us there before reaching the actual target page.
    """
    # ── Strategy 1: profile → Message button ──────────────────────────
    profile_url = f"https://www.instagram.com/{prospect_handle}/"
    log.info("m7.io.send.strategy_profile", url=profile_url)
    try:
        await page.goto(profile_url, timeout=20_000, wait_until="domcontentloaded")
        await _humanize_sleep(
            PACING["between_actions_min"], PACING["between_actions_max"]
        )
        # IG sometimes redirects to "Continue as <user>" before serving the
        # profile. Click through if so, then re-navigate.
        if await _dismiss_continue_gate(page):
            await page.goto(profile_url, timeout=20_000, wait_until="domcontentloaded")
            await _humanize_sleep(
                PACING["between_actions_min"], PACING["between_actions_max"]
            )
        msg_btn = page.locator(SELECTORS["profile_message_button"]).first
        await msg_btn.wait_for(state="visible", timeout=10_000)
        await msg_btn.click()
        await page.wait_for_selector(SELECTORS["message_input"], timeout=15_000)
        log.info("m7.io.send.compose_ready", strategy="profile_button")
        return
    except PWTimeoutError as e:
        log.warning(
            "m7.io.send.strategy_profile_failed",
            err=str(e),
            current_url=page.url,
        )
    except Exception as e:
        log.warning(
            "m7.io.send.strategy_profile_error",
            err=str(e),
            current_url=page.url,
        )

    # ── Strategy 2: direct thread URL ─────────────────────────────────
    thread_url = f"https://www.instagram.com/direct/t/{prospect_handle}/"
    log.info("m7.io.send.strategy_direct_url", url=thread_url)
    try:
        await page.goto(thread_url, timeout=20_000, wait_until="domcontentloaded")
        await _humanize_sleep(
            PACING["between_actions_min"], PACING["between_actions_max"]
        )
        if await _dismiss_continue_gate(page):
            await page.goto(thread_url, timeout=20_000, wait_until="domcontentloaded")
            await _humanize_sleep(
                PACING["between_actions_min"], PACING["between_actions_max"]
            )
        await page.wait_for_selector(SELECTORS["message_input"], timeout=15_000)
        log.info("m7.io.send.compose_ready", strategy="direct_url")
        return
    except PWTimeoutError as e:
        log.warning(
            "m7.io.send.strategy_direct_url_failed",
            err=str(e),
            current_url=page.url,
        )

    # ── All strategies failed: screenshot for forensic debugging ──────
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        shot = DEBUG_DIR / f"send_fail_{prospect_handle}_{ts}.png"
        await page.screenshot(path=str(shot), full_page=True)
        log.error(
            "m7.io.send.failure_screenshot",
            path=str(shot),
            prospect=prospect_handle,
            current_url=page.url,
        )
    except Exception as shot_err:
        log.error(
            "m7.io.send.screenshot_failed",
            err=str(shot_err),
            prospect=prospect_handle,
        )

    raise RuntimeError(
        f"DM compose unreachable for @{prospect_handle} — "
        f"both profile-button and direct-URL strategies failed. "
        f"See screenshot in {DEBUG_DIR} for what IG rendered."
    )


async def send_dm(handle: str, prospect_handle: str, text: str) -> Optional[str]:
    """
    Send a DM from `handle` to `prospect_handle` containing `text`.

    Returns IG message ID (or None — IG web doesn't expose stable IDs).
    Raises on hard failure.
    """
    log.info("m7.io.send.start", handle=handle, prospect=prospect_handle, chars=len(text))

    async with _ig_session(handle) as (_ctx, page):
        await _open_dm_compose(page, prospect_handle)

        # Type humanlike
        input_loc = page.locator(SELECTORS["message_input"]).first
        await input_loc.click()
        await _humanize_sleep(PACING["between_actions_min"] / 2, PACING["between_actions_max"] / 2)

        # Use keyboard typing for natural rhythm
        await page.keyboard.type(text, delay=random.randint(40, 110))

        await _humanize_sleep(PACING["before_send_min"], PACING["before_send_max"])

        # Send — try button first, fall back to Enter key
        try:
            send_btn = page.locator(SELECTORS["send_button"]).first
            if await send_btn.count() > 0:
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")
        except Exception as e:
            log.warning("m7.io.send.button_click_failed_fallback_enter", err=str(e))
            await page.keyboard.press("Enter")

        # If a "Send message request" confirmation modal appears (when DMing
        # someone who doesn't follow you), accept it. Best-effort — many
        # accounts won't show this.
        try:
            req_btn = page.locator(SELECTORS["request_confirm_send"]).first
            if await req_btn.count() > 0:
                visible = await req_btn.is_visible()
                if visible:
                    await req_btn.click()
                    log.info("m7.io.send.request_confirmed", prospect=prospect_handle)
        except Exception:
            # Modal not present or selector ambiguous — ignore.
            pass

        # Wait for input to clear as confirmation of send
        try:
            await page.wait_for_function(
                "el => !el || (el.innerText || '').trim() === ''",
                arg=await input_loc.element_handle(),
                timeout=10_000,
            )
        except PWTimeoutError:
            log.warning("m7.io.send.confirm_clear_timeout — assuming sent", handle=handle)

        log.info("m7.io.send.done", handle=handle, prospect=prospect_handle)

    # Update usage counter
    _increment_dm_usage(handle)
    return None


def _increment_dm_usage(handle: str) -> None:
    """Bump today's dms_sent counter for this IG account."""
    sb = get_supabase()
    today = datetime.now(timezone.utc).date().isoformat()
    # Upsert pattern — assumes (ig_account, usage_date) UNIQUE constraint
    existing = (sb.table("ig_account_usage").select("id, dms_sent")
                .eq("ig_account", handle).eq("usage_date", today)
                .limit(1).execute()).data
    if existing:
        sb.table("ig_account_usage").update(
            {"dms_sent": (existing[0]["dms_sent"] or 0) + 1}
        ).eq("id", existing[0]["id"]).execute()
    else:
        sb.table("ig_account_usage").insert({
            "ig_account": handle,
            "usage_date": today,
            "dms_sent": 1,
        }).execute()


# ────────────────────────────────────────────────────────────────────
# Daily cap check (helper for worker)
# ────────────────────────────────────────────────────────────────────

def can_send_dm_today(handle: str) -> tuple[bool, int, int]:
    """Returns (allowed, sent_today, cap)."""
    sb = get_supabase()
    today = datetime.now(timezone.utc).date().isoformat()
    acct = (sb.table("ig_accounts").select("daily_caps,current_status")
            .eq("handle", handle).single().execute()).data
    if not acct or acct["current_status"] != "active":
        return False, 0, 0
    cap = (acct.get("daily_caps") or {}).get("dms_sent", 25)
    usage = (sb.table("ig_account_usage").select("dms_sent")
             .eq("ig_account", handle).eq("usage_date", today)
             .limit(1).execute()).data
    sent = (usage[0]["dms_sent"] if usage else 0) or 0
    return sent < cap, sent, cap
