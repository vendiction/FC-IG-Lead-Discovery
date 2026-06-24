"""
Smoke test for M0–M3.

Runs without IG cookies. Tests what's testable offline + with stub data:
1. All modules import without errors
2. Settings load from .env
3. Supabase connection works (read-only query on tables we just created)
4. Fernet encrypt/decrypt roundtrip
5. Rate limiter logic (mock account, check + consume + reset)
6. Crawl queue: enqueue, claim, mark done, dedupe
7. Test mode gate works
8. mason_corpus table seeded correctly (right counts per category)
9. Website analyzer runs on a real public URL (apple.com or similar)
10. YouTube API key validates (one cheap search)
11. Schema sanity — every expected table exists

Usage:
    docker compose run --rm api python scripts/smoke_test.py
    OR
    python scripts/smoke_test.py   (with .env loaded and deps installed)

Exits 0 if all pass, 1 if any fail.
"""
from __future__ import annotations
import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSED, FAILED = [], []


def test(name: str):
    """Decorator: run a test function, capture pass/fail."""
    def deco(fn):
        async def run():
            try:
                result = fn()
                if asyncio.iscoroutine(result):
                    result = await result
                print(f"  ✓ {name}")
                PASSED.append(name)
            except Exception as e:
                print(f"  ✗ {name}")
                print(f"      {type(e).__name__}: {e}")
                tb = traceback.format_exc().splitlines()
                for line in tb[-4:]:
                    print(f"      {line}")
                FAILED.append((name, str(e)))
        return run
    return deco


# ============================================================================
# Group 1: Imports
# ============================================================================
@test("All modules import cleanly")
def test_imports():
    from app.core import settings, logging, supabase_client, crypto, rate_limiter, ig_session, test_mode  # noqa
    from app.modules.m1_tagged_crawler import ig_api, queue, repository, crawler, worker  # noqa
    from app.modules.m2_hashtag_scraper import ig_api as m2_ig, scraper, worker as m2_worker  # noqa
    from app.modules.m3_cross_platform import tiktok, youtube, website, research, worker as m3_worker  # noqa
    from app.api import main  # noqa


# ============================================================================
# Group 2: Settings
# ============================================================================
@test("Settings load from .env")
def test_settings():
    from app.core.settings import get_settings
    s = get_settings()
    assert s.supabase_url, "SUPABASE_URL missing"
    assert s.supabase_service_role_key, "SUPABASE_SERVICE_ROLE_KEY missing"
    assert s.fernet_key, "FERNET_KEY missing"
    assert s.llm_mode in ("api", "manual_paste"), f"LLM_MODE invalid: {s.llm_mode}"
    assert 0 < s.rate_limit_safety_factor <= 1.0, "safety factor out of range"


@test("Settings — TEST_MODE config sane")
def test_settings_test_mode():
    from app.core.settings import get_settings
    s = get_settings()
    if s.test_mode:
        assert s.test_mode_profile_limit > 0, "TEST_MODE_PROFILE_LIMIT must be > 0"
        print(f"      (test mode ON, limit={s.test_mode_profile_limit})")


# ============================================================================
# Group 3: Crypto
# ============================================================================
@test("Fernet encrypt/decrypt roundtrip")
def test_crypto():
    from app.core.crypto import encrypt_dict, decrypt_dict
    payload = {"cookies": [{"name": "sessionid", "value": "secret_abc"}]}
    token = encrypt_dict(payload)
    assert isinstance(token, str) and len(token) > 50
    assert "sessionid" not in token, "encrypt leaked plaintext!"
    recovered = decrypt_dict(token)
    assert recovered == payload


# ============================================================================
# Group 4: Supabase + Schema
# ============================================================================
@test("Supabase connection")
def test_supabase_connection():
    from app.core.supabase_client import get_supabase
    sb = get_supabase()
    r = sb.table("ig_accounts").select("id", count="exact").limit(1).execute()
    # Even if no rows, count should not be None
    assert r.count is not None, "Supabase query returned no count — connection issue"


EXPECTED_TABLES = [
    "accounts", "conversations", "crawl_queue", "cross_platform_profiles",
    "daily_metrics", "error_log", "followups", "gap_analysis", "handoffs",
    "hashtags", "ig_account_usage", "ig_accounts", "mason_corpus",
    "messages", "openers", "qualified_prospects", "seeds", "tag_edges",
    "warming_actions",
]


@test(f"Schema — all {len(EXPECTED_TABLES)} expected tables exist")
def test_schema_tables():
    from app.core.supabase_client import get_supabase
    sb = get_supabase()
    missing = []
    for t in EXPECTED_TABLES:
        try:
            sb.table(t).select("*", count="exact").limit(0).execute()
        except Exception as e:
            missing.append(f"{t} ({str(e)[:50]})")
    if missing:
        raise AssertionError(f"Missing tables: {missing}")


@test("mason_corpus seeded — expected category counts")
def test_mason_corpus_seeded():
    from app.core.supabase_client import get_supabase
    sb = get_supabase()
    expectations = {
        "opener_personal_hook": 5,
        "opener_gap_hook": 6,
        "opener_cross_platform": 1,
        "opener_curiosity_phrase": 5,
        "escalation_example": 1,
        "invitation_example": 1,
        "action_example": 1,
        "micro_commitment": 4,
        "objection_uncertainty_reframe": 2,
        "objection_overwhelm_reframe": 1,
        "followup_template": 5,
        "anti_pattern": 6,
    }
    problems = []
    for cat, expected in expectations.items():
        r = (sb.table("mason_corpus").select("id", count="exact")
             .eq("category", cat).execute())
        actual = r.count or 0
        if actual < expected:
            problems.append(f"{cat}: expected ≥{expected}, got {actual}")
    if problems:
        raise AssertionError(" / ".join(problems))


# ============================================================================
# Group 5: Crawl Queue
# ============================================================================
@test("Crawl queue — enqueue, claim, mark done")
def test_crawl_queue():
    from app.modules.m1_tagged_crawler.queue import (
        enqueue, claim_next, mark_done, queue_depth
    )
    from app.core.supabase_client import get_supabase
    sb = get_supabase()

    test_handle = "smoke_test_handle_xyz_999"
    # Clean any leftover
    sb.table("crawl_queue").delete().eq("handle", test_handle).execute()

    # Enqueue
    ok = enqueue(test_handle, depth=0, parent_seed="smoke_seed", priority=9)
    assert ok, "first enqueue should return True"

    # Dupe rejected
    ok2 = enqueue(test_handle, depth=0, parent_seed="smoke_seed", priority=9)
    assert not ok2, "duplicate enqueue should return False"

    # Claim
    row = claim_next("smoke_worker_1")
    # May be None if another item ranks higher, but if it returned, should be our test row OR something
    if row and row["handle"] == test_handle:
        mark_done(row["id"])

    # Cleanup
    sb.table("crawl_queue").delete().eq("handle", test_handle).execute()


# ============================================================================
# Group 6: Rate Limiter (mock account)
# ============================================================================
@test("Rate limiter — check_and_consume + cooldown")
def test_rate_limiter():
    from app.core.supabase_client import get_supabase
    from app.core.rate_limiter import (
        check_and_consume, mark_soft_block,
        RateLimitExceeded, AccountUnavailable,
    )
    sb = get_supabase()

    mock_handle = "smoke_test_acct_zzz"
    # Cleanup any leftovers
    sb.table("ig_account_usage").delete().eq("ig_account", mock_handle).execute()
    sb.table("ig_accounts").delete().eq("handle", mock_handle).execute()

    # Insert mock account
    sb.table("ig_accounts").insert({
        "handle": mock_handle,
        "proxy_endpoint": "direct://test",
        "daily_caps": {"follows": 10, "profile_loads": 5, "likes": 0,
                       "comments": 0, "story_actions": 0,
                       "hashtag_pages": 0, "dms_sent": 0},
        "current_status": "active",
    }).execute()

    try:
        # Consume up to limit (5 × 0.7 safety = 3, or 5 if safety = 1.0)
        # Use a small cap so we don't need to know safety factor
        from app.core.settings import get_settings
        cap = int(5 * get_settings().rate_limit_safety_factor)
        for i in range(cap):
            check_and_consume(mock_handle, "profile_loads", 1)

        # Next one should fail
        try:
            check_and_consume(mock_handle, "profile_loads", 1)
            raise AssertionError("expected RateLimitExceeded")
        except RateLimitExceeded:
            pass

        # Soft block sets account to cooldown
        mark_soft_block(mock_handle, hours=1)
        try:
            check_and_consume(mock_handle, "follows", 1)
            raise AssertionError("expected AccountUnavailable after soft block")
        except AccountUnavailable:
            pass
    finally:
        sb.table("ig_account_usage").delete().eq("ig_account", mock_handle).execute()
        sb.table("ig_accounts").delete().eq("handle", mock_handle).execute()


# ============================================================================
# Group 7: Test Mode Gate
# ============================================================================
@test("Test mode — at_test_limit() returns sensible value")
def test_test_mode_gate():
    from app.core.test_mode import at_test_limit, accounts_remaining
    from app.core.settings import get_settings
    s = get_settings()
    # Just exercise the code paths
    limit = at_test_limit()
    remaining = accounts_remaining()
    assert isinstance(limit, bool)
    assert isinstance(remaining, int)
    if s.test_mode:
        assert remaining <= s.test_mode_profile_limit


# ============================================================================
# Group 8: Website Analyzer
# ============================================================================
@test("Website analyzer — handles unreachable URL gracefully")
async def test_website_unreachable():
    from app.modules.m3_cross_platform.website import analyze_website
    result = await analyze_website("https://this-domain-definitely-does-not-exist-9999.invalid")
    assert result["has_website"] is False
    assert result["fetched_url"] is None


@test("Website analyzer — runs on real URL and detects basic structure")
async def test_website_real():
    from app.modules.m3_cross_platform.website import analyze_website
    # Apple has a clear marketing site we can analyze without surprises
    result = await analyze_website("https://www.apple.com")
    assert result["has_website"] is True
    assert isinstance(result["has_email_capture"], bool)
    assert isinstance(result["has_paid_offer"], bool)
    print(f"      (apple.com: has_email_capture={result['has_email_capture']}, "
          f"has_paid_offer={result['has_paid_offer']})")


# ============================================================================
# Group 9: YouTube API
# ============================================================================
@test("YouTube API key validates with cheap call")
async def test_youtube_api():
    from app.core.settings import get_settings
    import httpx
    s = get_settings()
    if not s.youtube_api_key:
        print("      (skipped — no YOUTUBE_API_KEY set)")
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "q": "test", "maxResults": 1,
                    "type": "channel", "key": s.youtube_api_key},
        )
    assert r.status_code == 200, f"YouTube API returned {r.status_code}: {r.text[:200]}"


# ============================================================================
# Group 10: Anthropic API (only if mode=api)
# ============================================================================
@test("Anthropic API key validates (only if LLM_MODE=api)")
async def test_anthropic_api():
    from app.core.settings import get_settings
    s = get_settings()
    if s.llm_mode == "manual_paste":
        print("      (skipped — LLM_MODE=manual_paste)")
        return
    if not s.anthropic_api_key or s.anthropic_api_key.startswith("skip"):
        print("      (skipped — no real ANTHROPIC_API_KEY)")
        return
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": s.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": s.claude_model_qualifier,
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "say ok"}],
            },
        )
    assert r.status_code == 200, f"Anthropic API returned {r.status_code}: {r.text[:200]}"


# ============================================================================
# Group 11: IG Session bootstrap (verifies Playwright + Chromium installed)
# ============================================================================
@test("Playwright + Chromium can launch a browser")
async def test_playwright_launches():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.set_content("<h1>smoke</h1>")
        text = await page.text_content("h1")
        assert text == "smoke"
        await browser.close()


# ============================================================================
# Runner
# ============================================================================
async def main():
    print("\n" + "=" * 60)
    print("M0–M3 SMOKE TEST")
    print("=" * 60)

    tests = [
        test_imports,
        test_settings,
        test_settings_test_mode,
        test_crypto,
        test_supabase_connection,
        test_schema_tables,
        test_mason_corpus_seeded,
        test_crawl_queue,
        test_rate_limiter,
        test_test_mode_gate,
        test_website_unreachable,
        test_website_real,
        test_youtube_api,
        test_anthropic_api,
        test_playwright_launches,
    ]

    for t in tests:
        await t()

    print("\n" + "=" * 60)
    print(f"PASSED: {len(PASSED)} / {len(PASSED) + len(FAILED)}")
    if FAILED:
        print(f"FAILED: {len(FAILED)}")
        for name, _ in FAILED:
            print(f"  - {name}")
        print("=" * 60)
        sys.exit(1)
    print("All green. Safe to continue building.")
    print("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
