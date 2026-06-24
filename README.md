# FC IG Lead Discovery & Conversation Engine

End-to-end Instagram lead-gen + cold-DM sales engine implementing Mason's Way: discovery → cross-platform research → qualification → warmup → S.I.P.E. opener → conversational selling → human handoff. AI does the thinking; a human operator does the IG clicking.

> **Status:** Operator-mode V1. Architecture validated end-to-end on 2026-06-24. **System runs; lead volume gated by IG burner health — see the caveat below.**

---

## ⚠️ Before you do anything: the burner caveat

This system needs **one healthy Instagram burner account** to do its read-only IG work (profile lookups, hashtag scrapes, tagged-photo crawls, inbox polls). All IG **write** actions (follow, like, comment, DM send, reply) go through a Discord queue to a human operator who executes them by hand. That's by design — IG bans automation hard.

"Healthy burner" means: created on a real phone over cellular data, warmed manually for 5–7 days (real bio, real posts, real follows, daily scrolling), and routed through a dedicated residential/mobile proxy. Datacenter proxies and browser-created accounts get banned within minutes.

**Without a healthy burner, no real prospects flow.** Full protocol in [`HANDOFF.md`](./HANDOFF.md) section 0.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  DISCOVERY (read-only, burner-driven)                                    │
│    M1  Tagged photo crawler ──┐                                          │
│    M2  Hashtag scraper       ─┴──>  accounts                             │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  M3  Cross-platform research (TikTok + IG + YouTube + website)           │
│       → gap_analysis, cross_platform_profiles                            │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  M4  Qualifier + scorer (pre-filter, link crawl, total_score)            │
│       → qualified_prospects                                              │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  M5  Warmup planner (follow, like, story, comment — all operator-routed) │
│  M6  S.I.P.E. opener generator (Claude Sonnet 4.6)                       │
│       → warming_actions, openers                                         │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  M8  Discord HiTL surface (operator-mode)                                │
│       🟢 Follow · 💛 Like · ❤️ Story · 💬 Comment · 📨 DM cards          │
│       Operator does the action on IG app → /follow_done /dm_sent etc.    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   ▼ (when prospect replies)
┌─────────────────────────────────────────────────────────────────────────┐
│  M7  Conversational engine (stateful Claude agent)                       │
│       Selling Map: opener → escalation → invitation → action             │
│       Validator enforces Mason's rules; low-confidence → handoff         │
│       → pending_outbound_messages (operator pastes) OR handoffs          │
└─────────────────────────────────────────────────────────────────────────┘

M0  Infrastructure: Supabase, Anthropic, Discord, proxies, Docker Compose
```

The key architectural commitment: **every IG-write action goes through the operator**. The burner only does reads. This trades raw scale for ban-resistance.

---

## Stack

- **Language:** Python 3.11
- **IG automation (reads only):** Playwright + playwright-stealth, encrypted session cookies
- **Proxies:** one residential/mobile endpoint per burner, stored in `ig_accounts.proxy_endpoint`
- **Storage:** Supabase (Postgres + REST)
- **LLM:** Anthropic Claude (Sonnet 4.6 for openers + agent, Haiku 4.5 for qualifier)
- **Web framework:** FastAPI (internal API)
- **Scheduler:** APScheduler (cron loops for followups, cooldown reaping, daily metrics)
- **HiTL UI:** Discord bot (discord.py) — 20 slash commands, 7 card types
- **Cross-platform research:** YouTube Data API v3 (free tier), TikTok public web scrape
- **Deploy:** Docker Compose; runs on a 2 vCPU / 4 GB Ubuntu host

---

## Module status

| Module | Description | Status |
|---|---|---|
| M0 | Infrastructure (Supabase + Docker + 9 services) | ✅ Live |
| M1 | Tagged-photo crawler | ✅ Built + validated against real IG |
| M2 | Hashtag scraper | ✅ Built + 16 seeded hashtags across 4 niches |
| M3 | Cross-platform research (TikTok + IG + YouTube + site) | ✅ Built + 100% TikTok hit rate, 93% YouTube |
| M4 | Qualifier + scorer | ✅ Built + scoring validated |
| M5 | Warmup planner (operator-routed) | ✅ Built; planner schedules to Discord queue |
| M6 | S.I.P.E. opener generator (Claude) | ✅ Built + Mason-quality output validated |
| M7 | Conversational engine + validator | ✅ Built + Claude agent + validator both fired live |
| M8 | Discord HiTL: cards, handoffs, input surface | ✅ Built; 20 slash commands, 7 card types |
| M9 | Monitoring dashboard | ⚠️ Stub only — not built |

130 / 130 tests passing as of 2026-06-24.

---

## Repo layout

```
fc-ig-lead-discovery/
├── app/
│   ├── core/                  # settings, supabase_client, scheduler, rate_limiter, input_normalize
│   ├── db/                    # repositories
│   ├── api/                   # FastAPI (internal endpoints)
│   └── modules/
│       ├── m1_tagged_crawler/
│       ├── m2_hashtag_scraper/
│       ├── m3_cross_platform/
│       ├── m4_qualifier/
│       ├── m5_warmup/
│       ├── m6_opener/         # sender (operator-mode, no Playwright write)
│       ├── m6_opener_generator/  # SIPE generator (Claude)
│       ├── m7_conversation/   # io_dm (read-only inbox poll), worker, validator
│       ├── m8_handoff/        # discord_bot — handoffs + operator cards + input surface
│       └── workers/           # shared worker entrypoints
├── migrations/                # 001…006 SQL migrations
├── docker/                    # Dockerfile (Playwright base)
├── scripts/                   # capture_session, approve_openers, smoke_test, clear_global_commands
├── tests/                     # 130 tests
├── docker-compose.yml
├── .env.example
├── HANDOFF.md                 # full handoff doc — read this for setup
└── README.md
```

---

## Quick start

Full setup is in [`HANDOFF.md`](./HANDOFF.md). Short version, assuming Docker + a filled-in `.env`:

```bash
# 1. Apply all 6 migrations to your Supabase project (SQL Editor)

# 2. Bring up the stack
docker compose up -d

# 3. Verify all 9 services are healthy
docker compose ps                            # all should show "Up"
docker compose logs --tail=10 discord_bot    # look for "commands_synced count=20 scope=guild"

# 4. Smoke test (does not require a working burner)
docker compose exec api python scripts/smoke_test.py
```

Once the stack is up, **the operator workflow lives entirely in Discord**:

```
# Seed discovery inputs:
/seed_add handles:drmarkhyman,drmindypelz niche:health_coaching
/hashtag_add tags:perimenopausecoach,hormonecoach niche:health_coaching

# See what's pending at any moment:
/queue_all

# Work the queue (cards stream into the channel):
/follow_done <id>       # for 🟢 Follow cards
/dm_sent <opener_id>    # for 📨 SEND DM cards
/reply_sent <pending_id>  # for 💬 INBOUND REPLY cards
/claim <handoff_id>     # for 🚨 Handoff cards
```

Discovery starts producing real prospects within ~10 minutes of seeding, **provided the burner is healthy** (see caveat above). When the pipeline finds, researches, qualifies, and writes an opener for a prospect, a 📨 SEND DM card lands in Discord with the Mason-quality opener pre-drafted and ready to long-press copy.

---

## Operating costs (rough, monthly, one-burner solo)

- Hostinger VPS: $7
- Supabase free tier: $0
- Anthropic Claude: $15–60 (depends on volume)
- YouTube API: $0 (free tier covers it)
- Residential proxy: $15–40
- **Total: ~$35–110/mo**

---

## Where to read next

- [`HANDOFF.md`](./HANDOFF.md) — full setup, dependency map, runbook, common failures
- [`.env.example`](./.env.example) — every env var with inline docs
- `migrations/*.sql` — schema reference
- `app/modules/m8_handoff/discord_bot.py` — every slash command and card type
