# M0 Setup — $0 / Burner Account Mode

This is the **zero-cost validation path**. Use this to prove the system works end-to-end before spending anything. Once you've made first sale, swap to the production M0 runbook (proxies + aged accounts).

## What you're committing to in this mode

- **1 IG burner account** (NOT your personal, NOT @fascinatecopy)
- **No proxy** — your home IP or mobile hotspot
- **Free API tiers only** — Claude Max (paste mode), free YouTube quota, free Supabase
- **Hard cap of 10 prospects** for the first validation pass
- **Ultra-conservative rate caps** — 30 profile loads/day instead of 150

## 1. Supabase (free tier)

1. Create new Supabase project at supabase.com (free tier is 500MB, plenty for 10 profiles).
2. SQL Editor → run `migrations/001_initial_schema.sql`
3. SQL Editor → run `migrations/002_mason_corpus.sql`
4. Settings → API → copy URL + service_role key → put in `.env`.

## 2. Free API keys

**Anthropic API:** SKIP. You'll use `LLM_MODE=manual_paste` — system writes prompts to a Supabase table, you paste into Claude.ai (Claude Max subscription), paste the response back.

**YouTube Data API v3 (free, 10k units/day):**
1. console.cloud.google.com → new project → enable YouTube Data API v3
2. Credentials → API key → copy → put in `.env` as `YOUTUBE_API_KEY`
3. (Restrict key to YouTube Data API only, for safety)

## 3. Generate Fernet encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Put in `.env` as `FERNET_KEY`. **Save it elsewhere too — losing it means re-capturing the IG cookies.**

## 4. The burner IG account

If you don't have one already:

1. Create a fresh IG account on your phone (use a real-looking name and bio — "marketing nerd, sharing what I learn" type vibe)
2. Add a profile pic
3. Post 5–10 real-looking posts over 1–2 weeks before automating anything (this is "warming" — IG trusts older + active accounts more)
4. Follow 30–50 real accounts in your target niche organically
5. Get to at least 50 followers via normal engagement

If you already have a dormant account from a past project, **use that** — it's already warm and looks more legit.

**DO NOT use:**
- Your personal IG
- The @fascinatecopy account
- Any account tied to your real identity, phone number, or business

## 5. No proxy — direct connection

You're using your home IP. To mitigate risk:

- **Run from your VPS, not your home computer.** The VPS IP is already used for your other services (n8n, etc.) but it's a dedicated server IP, less likely to be flagged than a residential ISP IP that maybe has other IG users on it.
- **Or run from your home PC** if you want belt-and-suspenders — but understand that IG action-block on your home IP could affect your personal account if they share the IP.

In `ig_accounts` table, put a placeholder proxy that points to nowhere (the code will detect this and skip proxy config):

```sql
INSERT INTO ig_accounts (handle, proxy_endpoint, has_blue_check, notes, daily_caps) VALUES
('your_burner_handle', 'direct://no-proxy', false, 'burner test account, no proxy', '{
  "follows": 5,
  "likes": 10,
  "comments": 0,
  "story_actions": 5,
  "profile_loads": 30,
  "hashtag_pages": 10,
  "dms_sent": 5
}'::jsonb);
```

Note the caps are way below the production defaults. These are designed for one home-IP account doing low volume.

> **Code note:** I need to make `ig_session.py` skip proxy if `proxy_endpoint` starts with `direct://`. That patch is in the next archive — see below.

## 6. Capture cookies

```bash
docker compose run --rm api python scripts/capture_session.py --handle your_burner_handle
```

Log in manually in the browser window. Press Enter when home feed loads. Cookies are Fernet-encrypted and stored.

## 7. Seed minimal data

```sql
-- Just 3 seeds for testing (you, plus 2 others you know are in target niche)
INSERT INTO seeds (handle, niche, notes) VALUES
  ('your_seed_handle_1', 'ecom', 'test seed'),
  ('your_seed_handle_2', 'coaching', 'test seed'),
  ('your_seed_handle_3', 'info', 'test seed');

-- 2 hashtags for M2 testing
INSERT INTO hashtags (tag, niche, source) VALUES
  ('learndropshipping', 'dropship', 'manual'),
  ('ecomfounder', 'ecom', 'manual');
```

## 8. .env settings for $0 mode

Critical values:

```bash
TEST_MODE=true
TEST_MODE_PROFILE_LIMIT=10
RATE_LIMIT_SAFETY_FACTOR=0.2          # ultra-safe for solo burner
LLM_MODE=manual_paste                  # use Claude.ai, not API
ENVIRONMENT=dev                        # nicer logs

# Anthropic key not required in manual_paste mode but harmless to set
ANTHROPIC_API_KEY=skip_in_manual_paste_mode

# YouTube key required for M3
YOUTUBE_API_KEY=your_actual_key

# Discord — optional in test mode (M5/M8 placeholder workers don't use it)
DISCORD_BOT_TOKEN=
```

## 9. Build and run

```bash
cd fc-ig-lead-discovery
docker compose build      # ~5–10 min first time (downloads Chromium)
docker compose up -d
docker compose logs -f api
```

`/health` should return `{"status":"ok"}`.

## 10. Kick off discovery

```bash
# Enqueue seeds
docker compose exec worker_disc python -m app.modules.m1_tagged_crawler.worker seed

# Watch logs in real-time
docker compose logs -f worker_disc
```

You should see entries like:
```
{"event":"m1.process.start","handle":"your_seed_1","depth":0}
{"event":"m1.process.done","handle":"your_seed_1","tagged_posts":18,"new_handles_found":12,"newly_queued":9,"remaining_budget":9}
```

Once `remaining_budget` hits 0, no new accounts will be enqueued. Existing queue entries finish processing, then the worker idles.

## 11. Validate the data

```sql
SELECT COUNT(*) FROM accounts;                    -- should be ≤10
SELECT handle, discovered_via, depth, follower_count
  FROM accounts ORDER BY first_seen_at;
SELECT * FROM tag_edges LIMIT 20;                 -- should show the rabbit hole
SELECT handle, primary_gap, gap_evidence
  FROM gap_analysis g JOIN accounts a ON a.id = g.account_id;
```

## Acceptance criteria for $0 M0

- [ ] Burner IG account warmed for ≥2 weeks, ≥50 followers, ≥5 posts
- [ ] Supabase + both migrations done
- [ ] Fernet key generated and saved somewhere safe
- [ ] YouTube API key working (test: `curl "https://www.googleapis.com/youtube/v3/search?part=snippet&q=test&key=$YOUTUBE_API_KEY"`)
- [ ] `capture_session.py` ran successfully — sessionid cookie stored
- [ ] `.env` has TEST_MODE=true, RATE_LIMIT_SAFETY_FACTOR=0.2, LLM_MODE=manual_paste
- [ ] 3 seeds and 2 hashtags inserted
- [ ] `docker compose up -d` runs without errors
- [ ] After `seed` command, M1 produces ≥5 accounts within an hour
- [ ] Zero soft-blocks during 48-hour burn-in
- [ ] No "action blocked" or "challenge_required" in logs

When all checked, $0 M0 is done.

## What "done with test mode" looks like

You'll have ~10 prospects in your DB, each with:
- Their IG profile data (bio, followers, link)
- Whether they have TikTok / YouTube
- Whether they have a website + email capture + paid offer
- Their `primary_gap` (Mason's gap framework applied)
- An evidence string explaining why that gap was picked

That's enough data to validate M4 (qualifier) and M6 (opener) when those modules land. You'll be able to read each row and judge: "would I actually send a DM to this person? does Mason's opener template make sense for this gap?"

If the answer is yes on 7+/10, the system works. Then you scale up: buy proxies, add 2 more aged accounts, flip TEST_MODE to false, run for real.

## Switching off test mode later

When ready to scale:

```bash
# In .env:
TEST_MODE=false
RATE_LIMIT_SAFETY_FACTOR=0.7
LLM_MODE=api                          # if you've decided to pay for Anthropic API
```

Restart containers. No code changes, no DB migration. Discovery resumes and ramps up.
