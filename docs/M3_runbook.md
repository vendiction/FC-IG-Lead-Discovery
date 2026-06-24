# M3 — Cross-Platform Research & Gap Detection Runbook

Implements Mason's pre-DM research: finds the prospect on TikTok and YouTube, analyzes their website for the named gaps, picks a `primary_gap` that M6 will hook the opener on.

## Submodules

| File | Purpose |
|---|---|
| `tiktok.py` | Find TikTok account by trying candidate handles, scrape user object |
| `youtube.py` | YouTube Data API v3 search + channel stats + recency |
| `website.py` | Fetch landing page, run heuristic checks for Mason's gaps |
| `research.py` | Orchestrator — combines all three, picks primary_gap |
| `worker.py` | Loop: pull un-researched accounts, run research, write to gap_analysis |

## Mason's gaps (mapped to code)

| Mason's gap | Detection logic |
|---|---|
| No website | `external_url` either missing or returns >=400 |
| No email capture | Page lacks `<input type=email>`, no Mailchimp/Klaviyo/etc., no "subscribe"/"newsletter" |
| No lead magnet | No "free guide/ebook/checklist/template" copy on landing page |
| Local SEO gap | Address or phone present (needs local SEO) but no LocalBusiness schema |
| Homepage conversion gap | Missing H1, generic H1 ("welcome", "home"), or H1 lacks action verb |
| Product page competitor gap | E-com detected; first product page missing ≥2 of: reviews, urgency, social proof |
| Email revenue underperform | E-com + no email capture detected (the Mason flagship gap) |
| Content struggle | No /blog, /articles, newsletter platform link |

## Cross-platform discovery source

If a prospect has more TikTok or YouTube followers than IG followers AND posts actively there, M3 sets `cross_platform_discovery_source` to that platform. M6 uses this to trigger the Mason archetype: `"Saw you on TikTok, but hitting you on IG"`.

## primary_gap selection priority

Used by M6 to pick which Mason template to hook on:

1. `email_revenue_underperform` — e-com + no email capture (highest signal)
2. `lead_magnet_missing` — coaching/info profile with no lead magnet
3. `homepage_conversion` — weak hero on any landing page
4. `product_page_competitor` — e-com missing competitor-common elements
5. `local_seo` — local biz without proper schema
6. `content_struggle` — no blog/newsletter
7. `cross_platform_mismatch` — fallback if no website gap but stronger on TikTok/YouTube
8. `null` — no gap detected; M4 may still qualify via other signals

## Cost

YouTube Data API v3 free tier = 10,000 units/day. Each prospect costs ~104 units (1 search + 1 channels + 1 playlistItems). So ~95 prospects/day on free tier. Bump to paid if needed.

TikTok scraping is free but slow (~5–10s per prospect).

Website fetch is free but capped at 20s timeout.

## Commands

```bash
# Worker runs automatically in worker_qual service. Inspect:
docker compose logs -f worker_qual

# One-off run for a single account:
docker compose exec worker_qual python -c "
import asyncio
from app.core.supabase_client import get_supabase
from app.modules.m3_cross_platform.research import research_account
sb = get_supabase()
acct = sb.table('accounts').select('*').eq('handle', 'test_handle').single().execute().data
asyncio.run(research_account(acct))
"

# Inspect a finished gap analysis:
docker compose exec worker_qual python -c "
from app.core.supabase_client import get_supabase
r = get_supabase().table('gap_analysis').select('*').limit(5).execute().data
import json; print(json.dumps(r, indent=2, default=str))
"
```

## Acceptance criteria

- [ ] Pick 20 random accounts from `accounts`, run M3 on each
- [ ] Spot-check: gap_analysis primary_gap matches your gut read on ≥70% of them
- [ ] Cross-platform discovery source correctly identified when prospect is bigger on TikTok/YouTube
- [ ] No 5xx errors blowing up the worker
- [ ] YouTube quota usage <10k/day (monitor in Google Cloud console)

## Known limitations

- TikTok lookup is heuristic — best-effort match on handle/name. Won't catch prospects with totally different platform names.
- Website analysis is single-page only. Skips multi-page funnels, gated checkouts, etc.
- "Email revenue underperform" is inferred (we don't have access to actual Klaviyo data). Detection = "they're e-com and we don't see email capture on landing." Mason's verbatim opener uses 20–30% revenue framing — M6 will phrase it that way regardless of certainty.
