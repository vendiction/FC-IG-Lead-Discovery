"""Playwright browser context per IG operator account.

Each account has:
- Its own residential proxy (sticky session)
- Its own persistent context dir (cookies, localStorage)
- Stealth patches applied

Acquired via async context manager. Sessions are reused across actions.
"""
from __future__ import annotations
import asyncio
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from playwright.async_api import async_playwright, BrowserContext, Page
try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None  # optional

from .supabase_client import get_supabase
from .crypto import decrypt_dict
from .logging import get_logger

log = get_logger(__name__)

SESSIONS_DIR = Path("/app/ig_sessions")
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Realistic mobile user-agents (IG is mobile-first; mobile pages get less scrutiny)
USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36",
]


async def human_sleep(min_s: float = 8.0, max_s: float = 45.0) -> None:
    """Randomized delay between any two IG actions."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def _account_session_dir(handle: str) -> Path:
    p = SESSIONS_DIR / handle
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_account_config(handle: str) -> dict:
    sb = get_supabase()
    r = (sb.table("ig_accounts")
         .select("handle,proxy_endpoint,session_cookies_encrypted")
         .eq("handle", handle)
         .single()
         .execute()).data
    if not r:
        raise ValueError(f"IG account {handle} not configured")
    return r


@asynccontextmanager
async def ig_session(handle: str, headless: bool = True) -> AsyncIterator[Page]:
    """
    Yields a Playwright Page logged in as `handle`, routed through its proxy,
    with stealth patches applied and saved cookies loaded.

    Usage:
        async with ig_session("our_operator_1") as page:
            await page.goto("https://www.instagram.com/...")
    """
    cfg = _load_account_config(handle)
    session_dir = _account_session_dir(handle)

    proxy_endpoint = cfg["proxy_endpoint"]  # "http://user:pass@host:port" or "direct://"
    proxy_config = _parse_proxy(proxy_endpoint)

    log.info("ig.session.start", handle=handle,
             proxy_host=(proxy_config["server"] if proxy_config else "DIRECT"))

    async with async_playwright() as pw:
        launch_kwargs = dict(
            user_data_dir=str(session_dir),
            headless=headless,
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 390, "height": 844},  # iPhone 14 Pro
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="en-US",
            timezone_id="America/Los_Angeles",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        if proxy_config:
            launch_kwargs["proxy"] = proxy_config

        context: BrowserContext = await pw.chromium.launch_persistent_context(**launch_kwargs)

        # Inject any decrypted cookies if first run.
        # Accepts both shapes seen in the wild:
        #   - {"cookies": [...]}  — legacy from older capture_session.py
        #   - [...]               — bare list, what capture_session.py writes now
        # Bug fix 2026-06-24: previously called .get() unconditionally, which
        # crashed with "'list' object has no attribute 'get'" against bare lists
        # and silently broke the entire M1/M2 IG session. Parallel fix to the
        # one already applied in app/modules/m7_conversation/io_dm.py.
        if cfg.get("session_cookies_encrypted"):
            try:
                decoded = decrypt_dict(cfg["session_cookies_encrypted"])
                if isinstance(decoded, dict) and "cookies" in decoded:
                    cookies = decoded["cookies"]
                elif isinstance(decoded, list):
                    cookies = decoded
                else:
                    raise RuntimeError(
                        f"decrypted cookies for @{handle} are "
                        f"{type(decoded).__name__}, expected list or "
                        f"{{cookies: list}} — stored blob is malformed"
                    )
                if cookies:
                    await context.add_cookies(cookies)
            except Exception as e:
                log.warning("ig.session.cookie_load_failed", handle=handle, err=str(e))

        page = await context.new_page()
        if stealth_async:
            await stealth_async(page)

        try:
            yield page
        finally:
            await context.close()
            log.info("ig.session.end", handle=handle)


def _parse_proxy(endpoint: str) -> dict | None:
    """Convert 'http://user:pass@host:port' to Playwright proxy config.
    Returns None for 'direct://' (no proxy) — used in $0 / burner-account mode."""
    if not endpoint or endpoint.startswith("direct://"):
        return None
    from urllib.parse import urlparse
    p = urlparse(endpoint)
    cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


async def detect_soft_block(page: Page) -> bool:
    """
    Check current page for IG action-block indicators.
    Call after any action that might trigger one.
    """
    body_text = (await page.text_content("body") or "").lower()
    block_phrases = [
        "action blocked",
        "try again later",
        "we restrict certain activity",
        "please wait a few minutes",
        "challenge_required",
        "checkpoint_required",
    ]
    return any(p in body_text for p in block_phrases)
