# Testing Runbook (M0–M3)

Two test scripts ship with the system:

## 1. `scripts/smoke_test.py` — offline + Supabase + APIs

Tests everything that doesn't need a live IG session. Run this first.

**What it checks (15 tests):**

| # | Test | What it proves |
|---|---|---|
| 1 | All modules import cleanly | No syntax errors, no missing deps, no circular imports |
| 2 | Settings load from .env | Pydantic Settings validates your env vars |
| 3 | TEST_MODE config sane | Test-mode values are internally consistent |
| 4 | Fernet crypto roundtrip | Encryption key works, cookies will be safe |
| 5 | Supabase connection | Service-role key works |
| 6 | All 19 tables exist | Migration 001 + 002 ran successfully |
| 7 | mason_corpus seeded | All 36+ verbatim templates loaded by category |
| 8 | Crawl queue enqueue/claim/dedupe | M1's persistent queue is functional |
| 9 | Rate limiter check/consume/cooldown | Daily caps enforce, soft-block sets cooldown |
| 10 | Test mode gate | The 10-profile cap is reachable |
| 11 | Website analyzer — unreachable URL | M3 fails gracefully on dead domains |
| 12 | Website analyzer — real URL (apple.com) | M3 produces sensible output on a known site |
| 13 | YouTube API key | Free-tier quota is alive |
| 14 | Anthropic API key (if mode=api) | Skipped in manual_paste mode |
| 15 | Playwright launches Chromium | Browser is installed and works |

**How to run:**

```bash
docker compose run --rm api python scripts/smoke_test.py
```

Expected output: `PASSED: 15 / 15` (or 13/15 with anthropic + youtube skipped if you don't have keys).

**If something fails:**
- Tests 1–4: code-level issue, paste me the error
- Test 5: bad SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in `.env`
- Test 6: migrations 001 and/or 002 not run — re-run them in Supabase SQL Editor
- Test 7: migration 002 was run but seed INSERTs were skipped — open it and check
- Tests 8–10: should never fail unless schema is broken; rerun migrations
- Test 11: should never fail; if it does, your VPS may be blocking outbound HTTPS
- Test 12: same as 11 but to apple.com specifically
- Test 13: bad YOUTUBE_API_KEY or YouTube API not enabled in Google Cloud
- Test 14: bad ANTHROPIC_API_KEY
- Test 15: Playwright didn't install Chromium during Docker build — rebuild image

## 2. `scripts/test_ig_live.py` — real IG endpoint test

Only run **after** smoke_test passes AND **after** you've captured a session via `capture_session.py`. Uses 1–2 profile_loads against your daily cap.

**What it does:**

1. Picks your first active IG operator account
2. Opens a real Playwright session through its proxy (or direct, in burner mode)
3. Navigates to instagram.com
4. Checks for soft-block on home feed
5. Fetches a public profile (default: `@instagram` — the official IG account)
6. Prints the normalized profile fields
7. Optionally fetches one page of their tagged-photos feed

**How to run:**

```bash
# Default: test against @instagram (safe, official, never bans testers)
docker compose run --rm worker_disc python scripts/test_ig_live.py

# Test against a specific handle
docker compose run --rm worker_disc python scripts/test_ig_live.py --handle nasa

# Skip tagged-photos to save quota
docker compose run --rm worker_disc python scripts/test_ig_live.py --no-tagged
```

**Expected output:**

```
Using operator account: your_burner_handle
Target profile: @instagram

Profile fetched successfully:
{
  "handle": "instagram",
  "full_name": "Instagram",
  "bio": "Bringing you closer to the people and things you love. ❤️",
  "follower_count": 670000000,
  "following_count": 246,
  ...
}

Testing tagged-photos endpoint (1 page only)...
  → 12 tagged posts found
  → 47 unique tagged users (sample 5): ['some_handle', ...]

✓ Live IG session test passed.
```

**If it fails:**
- "No active IG accounts" → run `capture_session.py` first
- "Rate limit rejected" → you've exhausted today's profile_loads; wait 24h
- "Soft block detected on home feed" → your operator account is cooked; rotate to another or wait 48h
- IG API error → IG's web frontend may have changed; paste me the error so I can patch ig_api.py

## 3. Spot-check the data after one real crawl

After smoke_test + live test both pass, seed your 3 seeds + 2 hashtags and let M1+M2 run for ~1 hour. Then inspect:

```bash
# In Supabase SQL Editor:

-- How many accounts did we find?
SELECT COUNT(*) FROM accounts;

-- How were they discovered?
SELECT discovered_via, COUNT(*)
FROM accounts GROUP BY discovered_via;

-- The rabbit hole graph (M1 working?)
SELECT from_handle, COUNT(*) as outbound_edges
FROM tag_edges
GROUP BY from_handle
ORDER BY outbound_edges DESC LIMIT 10;

-- Queue health
SELECT status, COUNT(*) FROM crawl_queue GROUP BY status;

-- Rate limit usage today
SELECT * FROM ig_account_usage
WHERE usage_date = CURRENT_DATE;

-- Any errors logged?
SELECT module, severity, message, occurred_at
FROM error_log ORDER BY occurred_at DESC LIMIT 20;
```

Once you see ≥5 accounts, ≥1 tag_edges row, and no entries in error_log, M0–M3 are confirmed working end-to-end. Then we can build M4.

## What "tested enough" looks like

Before continuing to M4:

- [ ] `smoke_test.py` returns 15/15 (or 13/15 with API skips)
- [ ] `test_ig_live.py` successfully fetches `@instagram` profile
- [ ] You've seeded 3 handles + 2 hashtags
- [ ] M1+M2 ran for at least 1 hour without soft-blocks
- [ ] ≥5 accounts in the `accounts` table
- [ ] M3 has produced ≥1 `gap_analysis` row
- [ ] Error log is empty

When all checked, ping me with "ready for M4" and I'll start building.
