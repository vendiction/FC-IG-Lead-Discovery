# Opener Sender Bridge — Operator Runbook

Built today as the unblocker for the choke point you confirmed via SQL:
M6 had generated 3 openers, all sat at `approved_for_send=false` with no
service to send them. Pipeline died there.

## What this bridge does

```
M6 generates opener
   ↓
[manual or scripted approval]   approved_for_send=true
   ↓
m6_opener.sender (NEW worker)   polls openers WHERE approved AND not sent
   ↓ preflight (celebrity cap, daily DM cap, account active)
   ↓
io_dm.send_dm (M7's existing Playwright layer)
   ↓
create conversation row, insert outbound, status='opener_ready'
   ↓
M7 already monitors active conversations for inbound replies
```

The approval mechanism is **just a DB flag** (`openers.approved_for_send`).
That means you can approve in 3 ways:

1. **CLI** (works now, no Discord needed): `scripts/approve_openers.py`
2. **Raw SQL** in Supabase: `UPDATE openers SET approved_for_send=true, approved_by='jon', approved_at=NOW() WHERE id='...';`
3. **Discord** (later, when you wire creds): the existing M8 bot can be extended with `/approve_opener` commands

## Files in this drop

```
migrations/002_opener_review.sql        # adds sent_at, send_failure_reason, discord_review_message_id + an index
app/modules/m6_opener/sender.py         # the actual sender worker
scripts/approve_openers.py              # CLI: list / show / approve / edit / reject / bulk ops
tests/test_m6_sender_preflight.py       # 8 tests, all pass
```

## Deploy steps

### 1. Apply the migration

In Supabase SQL Editor, paste the contents of `migrations/002_opener_review.sql`
and run. It's idempotent (uses `IF NOT EXISTS`). Verify:

```sql
SELECT column_name FROM information_schema.columns
WHERE table_name = 'openers' AND column_name IN ('sent_at', 'send_failure_reason', 'discord_review_message_id');
```
Should show 3 rows.

### 2. Add env vars to `.env`

```bash
# Opener sender (M6)
OPENER_SEND_MAX_FOLLOWER_COUNT=500000     # block sending to anyone above this
M6_SENDER_POLL_SECONDS=60                  # how often to scan for approved openers
M6_SENDER_BETWEEN_SENDS=180                # gap between sends (3min — human pacing)
M6_SENDER_BATCH_SIZE=5                     # max openers per sweep
```

### 3. Add the worker service to docker-compose.yml

```yaml
  worker_opener:
    <<: *common
    container_name: fc_ig_worker_opener
    command: python -m app.modules.m6_opener.sender
    depends_on:
      - api
```

Also add the tests mount under `x-common.volumes` (you saw this earlier):

```yaml
    - ./tests:/app/tests:ro
    - ./scripts:/app/scripts:ro     # NEW — so the CLI runs in the container
    - ./migrations:/app/migrations:ro   # NEW — optional, for reference
```

### 4. Build + start

```powershell
docker compose build worker_opener
docker compose up -d worker_opener
docker compose logs -f worker_opener
```

You should see:
```
m6.sender.start poll_seconds=60 between_sends=180 max_follower_count=500000
```

## How to approve the 3 existing openers

Two of them (mrbeast, hormozi) are celebrity-tier — the sender will refuse them
even if you approve, because of the safeguard. Recommended workflow:

```powershell
# 1. List pending openers
docker compose exec worker_opener python -m scripts.approve_openers list

# 2. Show full detail on the ettheh one
docker compose exec worker_opener python -m scripts.approve_openers show <id>

# 3. Reject the 2 celebrities outright
docker compose exec worker_opener python -m scripts.approve_openers reject-celebrities --cap 500000

# 4. The ettheh opener is at fc≈? — if under 500K, approve:
docker compose exec worker_opener python -m scripts.approve_openers approve <ettheh_opener_id> --by jon

# 5. Watch the sender pick it up
docker compose logs -f worker_opener
```

Within 60 seconds the sender should attempt the send. If your IG account has
working session cookies and proxy, the DM lands and the prospect status
flips to `opener_ready`. M7's `worker_conv` will then start checking that
prospect's inbox on its next poll cycle.

(Reality check: etthehiphoppreacher is also celebrity-ish for our purposes.
The safer move is to **reject all 3 existing openers, reseed M1 with real
target handles, let the rabbit hole find depth-1 prospects, then approve
those**. The 3 currently in the table were always seeds, never targets.)

## Failure modes you'll hit

| Symptom | Probable cause | Fix |
|---|---|---|
| `preflight: no active IG account available` | All `ig_accounts.current_status` rows are not 'active' | `UPDATE ig_accounts SET current_status='active' WHERE handle='ignorethisdump2';` |
| `preflight: daily DM cap reached` | Hit the cap in `ig_accounts.daily_caps.dms_sent` (default 25) | Wait until tomorrow or raise the cap in the row |
| `send_dm: DM input not found for @X` | IG DOM selector drift, OR the prospect blocked you, OR the session cookies expired | First check session cookies — re-encrypt fresh ones into `session_cookies_encrypted` |
| `m6.sender.no_ig_account_available` (no warming history fallback either) | No active IG accounts exist at all | Add one via your existing onboarding script |
| Sender shows `cycle_done succeeded=0` repeatedly | All pending openers above the follower cap, or all already sent | Expected — drop fresh ones in or lower the cap |

## What's deliberately NOT in this drop

- **No Discord review surface.** You said no Discord creds yet. When you have
  them, the next iteration adds `/list_openers /approve_opener /edit_opener
  /reject_opener` to the M8 bot. The DB schema (`discord_review_message_id`)
  already supports it, no further migration needed.
- **No auto-approval logic.** Every opener requires a human gate. Mason's
  whole philosophy is that openers are the most personal asset — auto-approve
  is exactly how you destroy account reputation. Even if you wanted it, the
  voice_match_score field is NULL on all 3 current openers (M6 isn't computing
  it yet), so there's no signal to gate on.
- **No M6 template bug fix.** The mrbeast opener has the literal string
  `homepage_conversion` in the rendered text — that's an M6 prompt/template
  issue, not a sender issue. Filed for next round.
- **No scheduler crash-loop fix.** `fc_ig_scheduler` is restarting per your
  `docker compose ps`. Separate bug, paste `docker compose logs scheduler`
  and we'll fix it.

## Test status

```bash
docker compose exec worker_opener bash -c "pip install --break-system-packages -q pytest pytest-asyncio && python -m pytest tests/test_m6_sender_preflight.py -v"
```

Expected: **8 passed**. Or run all tests: 53 total (32 M7 + 13 M5 vibe + 8 M6 preflight).
