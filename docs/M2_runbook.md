# M2 — Hashtag Scraper Runbook

Scrapes active hashtags from the `hashtags` table to find post authors who become candidates in `accounts`.

## How it works

For each active hashtag whose `last_scraped_at` is older than 24h (or null):
1. Fetch `top` section (~50 posts)
2. Fetch `recent` section (~100 posts)
3. Extract every unique post author
4. For each author not already in `accounts`:
   - Fetch their profile via the same IG session
   - Upsert into `accounts` with `discovered_via='hashtag'`, `discovered_from=<tag>`
   - Enqueue them into `crawl_queue` at depth=0 (M1 will then crawl their tagged photos)

## Rate limits

Per IG account, per day:
- `hashtag_pages`: 80 (× safety 0.7 = 56 effective)
- Plus `profile_loads` for each new author (shared budget with M1)

One hashtag scrape costs 2 `hashtag_pages` (top + recent) plus N `profile_loads` where N = unique new authors found (typically 30–80).

Soft-block → 48h cooldown on the operator account, like M1.

## Commands

```bash
# Add a hashtag to scrape
docker compose exec -T worker_disc python -c "
from app.core.supabase_client import get_supabase
get_supabase().table('hashtags').insert([
    {'tag': 'learndropshipping', 'niche': 'dropship', 'source': 'manual'},
    {'tag': 'ecomfounder', 'niche': 'ecom', 'source': 'manual'},
]).execute()
"

# The M2 worker runs automatically inside worker_disc. Check logs:
docker compose logs -f worker_disc | grep m2
```

## Acceptance criteria

- [ ] Each of your 5 seed hashtags has been scraped at least once
- [ ] Each scrape produces ≥80 unique authors written to `accounts`
- [ ] All M2-discovered handles also appear in `crawl_queue` so M1 can recurse on them
- [ ] No soft-blocks across 72-hour burn-in
