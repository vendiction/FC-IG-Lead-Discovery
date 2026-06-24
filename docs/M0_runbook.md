# M0 — Infrastructure Setup Runbook

This is the bootstrap sequence. Complete every step before moving to M1.

## 1. Supabase project

1. Create a new Supabase project (or reuse an existing FC project).
2. In the SQL Editor, paste `migrations/001_initial_schema.sql` and run it.
3. Verify all tables exist:
   ```sql
   SELECT table_name FROM information_schema.tables
   WHERE table_schema = 'public' ORDER BY table_name;
   ```
   Should show: `accounts`, `conversations`, `crawl_queue`, `cross_platform_profiles`,
   `daily_metrics`, `error_log`, `followups`, `gap_analysis`, `handoffs`, `hashtags`,
   `ig_account_usage`, `ig_accounts`, `messages`, `openers`, `qualified_prospects`,
   `seeds`, `tag_edges`, `warming_actions`.
4. Note the project URL and the **service_role** key (Settings → API). Put them in `.env`.

## 2. Anthropic API key

1. Get a key from console.anthropic.com (use the FC Anthropic account, not personal).
2. Put it in `.env` as `ANTHROPIC_API_KEY`.
3. Confirm Claude Sonnet 4.6 access. Run:
   ```bash
   curl https://api.anthropic.com/v1/messages \
     -H "x-api-key: $ANTHROPIC_API_KEY" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"claude-sonnet-4-6","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'
   ```

## 3. Generate Fernet encryption key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
Paste output into `.env` as `FERNET_KEY`. **Save it elsewhere too — losing it means losing all stored IG sessions.**

## 4. Procure residential proxies

- Recommended: Bright Data residential, IPRoyal residential, or Smartproxy
- Buy 3 **static** residential IPs (sticky sessions, not rotating-per-request)
- One IP will be permanently assigned to one IG account. Never share.
- Format the endpoint as `http://user:password@host:port`

## 5. Procure or warm IG operator accounts

**Option A — Buy aged accounts (faster, riskier):**
- Get from reputable sellers (e.g., AccsMarket, PVAcreator) — 6+ months old, real-looking
- Budget ~$50–80 each, 3 accounts to start
- Each gets a $20 IG blue checkmark per Mason's tactic (more visibility in requests folder)
- Set bios to look like real operators (FC-related, not generic)

**Option B — Warm fresh accounts (slower, safer):**
- Create 3 accounts on different mobile devices
- Post 15–20 real-looking posts over 30 days
- Build to 200+ followers organically
- Skip this if speed is the priority

## 6. Capture session cookies for each IG account

For each account:

```bash
# One-time interactive login to capture cookies
python scripts/capture_session.py --handle our_operator_1 --proxy "http://user:pass@host:port"
```

This will:
1. Open Playwright in non-headless mode through the assigned proxy
2. You log in manually (do NOT use any saved auto-fill)
3. Solve any captcha
4. Wait until home feed loads
5. Press Enter in terminal — cookies are extracted and Fernet-encrypted
6. Encrypted blob is inserted into `ig_accounts` row

## 7. Insert IG account records

```sql
INSERT INTO ig_accounts (handle, proxy_endpoint, has_blue_check, notes) VALUES
  ('our_operator_1', 'http://user:pass@brd.superproxy.io:22225', true, 'aged 2022, FC operator'),
  ('our_operator_2', 'http://user:pass@brd.superproxy.io:22226', true, 'aged 2023, FC operator'),
  ('our_operator_3', 'http://user:pass@brd.superproxy.io:22227', true, 'aged 2023, FC operator');
```

Then run capture_session.py for each.

## 8. Discord setup (optional but recommended for HiTL)

1. Create a Discord server "FC IG Ops" (or reuse existing FC server)
2. Create channels: `#ig-scraper-logs`, `#warming-queue`, `#hot-leads`, `#handoffs`, `#alerts`, `#metrics`
3. Create a bot at discord.com/developers, invite to server with permissions: read msgs, send msgs, manage msgs, embed links
4. Put bot token + channel IDs in `.env`

## 9. Seed your starting data

```sql
-- Seed accounts (your initial 10–20)
INSERT INTO seeds (handle, niche, notes) VALUES
  ('seed_handle_1', 'ecom', 'Mason mentioned'),
  ('seed_handle_2', 'coaching', '8-fig founder');
  -- ... etc

-- Starting hashtags
INSERT INTO hashtags (tag, niche, source) VALUES
  ('learndropshipping', 'dropship', 'manual'),
  ('ecomfounder', 'ecom', 'manual'),
  ('coachingbusiness', 'coaching', 'manual'),
  ('infomarketer', 'info', 'manual'),
  ('saasfounder', 'saas', 'manual');
```

## 10. Build and run the stack

```bash
cd fc-ig-lead-discovery
cp .env.example .env  # fill in all values
docker compose build
docker compose up -d api    # start API only first
docker compose logs -f api  # confirm /health returns ok
curl http://127.0.0.1:8001/health  # should return {"status":"ok"}
```

## M0 Acceptance Criteria

- [ ] Supabase schema migrated, all 18 tables present
- [ ] `.env` complete with no placeholder values
- [ ] 3 IG accounts inserted into `ig_accounts`, each with proxy + encrypted cookies
- [ ] `scripts/capture_session.py` confirmed working for at least one account
- [ ] API container running, `/health` returns 200
- [ ] Discord bot online in server (if using HiTL)
- [ ] Initial seed list (≥10 handles) + 5 hashtags inserted

When all 7 checkboxes are checked, M0 is done. Move to M1.
