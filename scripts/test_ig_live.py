"""
Live IG session test. Requires a captured session for at least one IG account.

Tests against real IG endpoints with strict rate limiting. Use sparingly —
each run costs 1–2 profile_loads against the daily cap.

Usage:
    docker compose run --rm worker_disc python scripts/test_ig_live.py --handle instagram

This will:
1. Pick the first 'active' ig_account
2. Load that account's session
3. Fetch the public profile of --handle (default: instagram, which is safe)
4. Print the normalized profile data
5. NOT enqueue, NOT recurse — read-only test
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.logging import configure_logging, get_logger
from app.core.ig_session import ig_session, human_sleep, detect_soft_block
from app.core.rate_limiter import get_active_accounts, check_and_consume
from app.modules.m1_tagged_crawler.ig_api import (
    get_profile, get_tagged_posts, extract_tagged_users,
    normalize_profile,
)

configure_logging()
log = get_logger("smoke_ig_live")


async def main(target_handle: str, do_tagged: bool):
    accounts = get_active_accounts()
    if not accounts:
        print("FAIL: no active IG accounts in ig_accounts table.")
        print("      Insert one and run scripts/capture_session.py first.")
        sys.exit(1)

    ig = accounts[0]
    print(f"Using operator account: {ig['handle']}")
    print(f"Target profile: @{target_handle}")
    print()

    # Reserve a profile load slot
    try:
        check_and_consume(ig["handle"], "profile_loads", 1)
    except Exception as e:
        print(f"FAIL: rate limiter rejected the test: {e}")
        sys.exit(1)

    async with ig_session(ig["handle"], headless=True) as page:
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await human_sleep(3, 7)

        # Quick health check
        if await detect_soft_block(page):
            print("FAIL: soft block detected on home feed — account may be cooked")
            sys.exit(1)

        # Fetch target
        try:
            user = await get_profile(page, target_handle)
        except Exception as e:
            print(f"FAIL: get_profile error: {type(e).__name__}: {e}")
            sys.exit(1)

        normalized = normalize_profile(user)
        normalized.pop("raw_json", None)  # too big to print
        print("Profile fetched successfully:")
        print(json.dumps(normalized, indent=2, default=str))
        print()

        if do_tagged:
            print("Testing tagged-photos endpoint (1 page only)...")
            try:
                check_and_consume(ig["handle"], "profile_loads", 1)
                posts = await get_tagged_posts(page, normalized["_pk"], max_posts=12)
                print(f"  → {len(posts)} tagged posts found")
                all_tagged = set()
                for p in posts:
                    for u in extract_tagged_users(p):
                        all_tagged.add(u["username"])
                print(f"  → {len(all_tagged)} unique tagged users (sample 5): "
                      f"{list(all_tagged)[:5]}")
            except Exception as e:
                print(f"  tagged fetch failed: {type(e).__name__}: {e}")
                # Not fatal — many profiles have no tagged content or it's private

    print()
    print("✓ Live IG session test passed.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--handle", default="instagram",
                   help="Public IG handle to test against (default: instagram)")
    p.add_argument("--no-tagged", action="store_true",
                   help="Skip the tagged-photos endpoint test")
    args = p.parse_args()
    asyncio.run(main(args.handle, not args.no_tagged))
