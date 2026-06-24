"""
Capture and encrypt session cookies for an IG operator account.

Usage:
    python scripts/capture_session.py --handle our_operator_1

The handle must already exist in ig_accounts table with a proxy_endpoint set.
Runs Playwright in non-headless mode so you can log in manually.
"""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path

# Make app/ importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright
from app.core.supabase_client import get_supabase
from app.core.crypto import encrypt_dict
from app.core.ig_session import _parse_proxy, _account_session_dir, USER_AGENTS
import random


async def capture(handle: str) -> None:
    sb = get_supabase()
    cfg = (sb.table("ig_accounts")
           .select("handle,proxy_endpoint")
           .eq("handle", handle)
           .single()
           .execute()).data

    if not cfg:
        print(f"ERROR: account {handle} not found in ig_accounts table.")
        print("Insert a row first with the proxy_endpoint set.")
        sys.exit(1)

    proxy = _parse_proxy(cfg["proxy_endpoint"])
    session_dir = _account_session_dir(handle)

    if proxy:
        print(f"Launching Chromium through proxy {proxy['server']} ...")
    else:
        print("Launching Chromium with NO PROXY (direct connection — burner mode)...")
    print(f"Persistent context: {session_dir}")

    async with async_playwright() as pw:
        launch_kwargs = dict(
            user_data_dir=str(session_dir),
            headless=False,
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 390, "height": 844},
            is_mobile=True,
            has_touch=True,
            locale="en-US",
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
        context = await pw.chromium.launch_persistent_context(**launch_kwargs)
        page = await context.new_page()
        await page.goto("https://www.instagram.com/accounts/login/")

        print("\n" + "=" * 60)
        print("LOG IN MANUALLY in the browser window.")
        print("Solve any captcha. Wait until the home feed loads.")
        print("Then return here and press Enter.")
        print("=" * 60)
        input()

        cookies = await context.cookies()
        if not any(c["name"] == "sessionid" for c in cookies):
            print("ERROR: no sessionid cookie found. Did login succeed?")
            await context.close()
            sys.exit(1)

        # Store the bare cookies list — that's what Playwright's add_cookies()
        # expects on the read side. Previously this wrapped as {"cookies": cookies}
        # which crashed io_dm with "expected array, got object". The io_dm
        # reader handles both shapes for backwards compat with any rows
        # written under the old format.
        encrypted = encrypt_dict(cookies)
        sb.table("ig_accounts").update({
            "session_cookies_encrypted": encrypted,
            "current_status": "active",
        }).eq("handle", handle).execute()

        print(f"✓ Captured {len(cookies)} cookies, encrypted and stored for {handle}.")
        await context.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--handle", required=True, help="IG account handle (must exist in ig_accounts)")
    args = p.parse_args()
    asyncio.run(capture(args.handle))


if __name__ == "__main__":
    main()
