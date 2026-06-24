# M1 — Tagged Photo Crawler Runbook

Implements Mason's "rabbit hole" tactic. Given a seed handle, fetches their tagged photos and recursively crawls everyone tagged in them, up to depth=2.

## How it works

```
seed (depth 0) ──> fetch profile + tagged feed
                ├── tagged user A (depth 1) ──> fetch profile + tagged feed
                │                            ├── tagged user A.1 (depth 2) ──> profile only, stop
                │                            └── tagged user A.2 (depth 2) ──> profile only, stop
                └── tagged user B (depth 1) ──> ...
```

- Depth-2 cap: at depth 2 we still fetch the profile (so the prospect lands in `accounts`) but don't recurse further. This already produces ~hundreds of accounts per seed.
- Dedupe: `accounts.handle` and `crawl_queue.handle` are both UNIQUE. Seeing the same handle via multiple paths is a no-op.
- Tag-edge graph: every `(from, to, post_url)` triple is recorded in `tag_edges` for later graph analysis ("which seeds produce the most qualified prospects?").

## Rate limits enforced

Per IG account, per day:
- `profile_loads`: 150 (× safety_factor 0.7 = 105 effective)
- Each crawl processes 1 profile + 1 tagged-feed pagination ≈ 1 unit
- With 3 accounts: ~315 crawls/day theoretical max

Soft-block detection: any "action blocked" / "try again later" → account goes into 48h cooldown, worker switches to the next active account automatically.

## Commands

All commands run inside the container:

```bash
# Seed the queue from the `seeds` table (run once after inserting seeds)
docker compose exec worker_disc python -m app.modules.m1_tagged_crawler.worker seed

# Enqueue a single handle ad-hoc
docker compose exec worker_disc python -m app.modules.m1_tagged_crawler.worker enqueue some_handle

# Check queue depth
docker compose exec worker_disc python -m app.modules.m1_tagged_crawler.worker status

# The worker loop runs automatically as the service — no command needed
```

## Acceptance criteria

Run on initial seed list (≥10 handles, depth=2), produces:

- [ ] ≥500 unique handles in `accounts` table within 72 hours
- [ ] Zero soft-blocks across 3-day burn-in (if you get a soft-block, the rate caps are too aggressive — lower `rate_limit_safety_factor` in .env)
- [ ] `tag_edges` queryable: `SELECT to_handle FROM tag_edges WHERE from_handle = 'your_seed_1' LIMIT 50;` returns 50 rows
- [ ] Logs in Docker show structured JSON entries: `{"event": "m1.process.done", "handle": "...", "new_handles_found": N}`

## What's NOT in M1

- No qualification scoring (that's M4)
- No bio analysis or filtering at this stage — we cast wide; M4 will filter
- No hashtag scraping (that's M2)
- No "tagged person's friends" beyond depth 2 (configurable in `crawler.py` as `MAX_DEPTH`)

## When to move on

Once M1 has produced ≥500 accounts and run cleanly for 72 hours, build M2 (hashtag scraper). M2 reuses the same `ig_session`, `rate_limiter`, and account-rotation code — should be quick.
