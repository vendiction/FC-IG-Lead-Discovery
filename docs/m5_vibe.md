# M5 Vibe Classifier — Integration Note

Built today as the third Mason gap from Task 2 ("fire emojis or professional
compliments depending on the target").

## Files

```
app/modules/m5_warmup/
├── vibe.py                  # Vibe Literal + VibeProfile dataclass
├── vibe_classifier.py       # classify_vibe() — heuristic + optional LLM
└── comment_suggestions.py   # COMMENT_TEMPLATES per vibe
```

13 unit tests against realistic IG samples — all passing.

## How M5's existing worker integrates this

When M5 schedules a comment warming action, it should call `classify_vibe`
once per prospect, store the result in `warming_actions.human_payload`, and
the Discord queue surfaces the comment style hint to whoever picks it up.

```python
from app.modules.m5_warmup.vibe_classifier import classify_vibe
from app.modules.m5_warmup.comment_suggestions import template_for

# In M5 worker — when creating a comment action for the human queue
def schedule_comment_action(prospect, ig_account, target_post_url):
    # Pull recent captions — assumes M3 cached them, or fetch on-demand
    recent_captions = _get_recent_captions_for(prospect["handle"])

    profile = classify_vibe(
        bio=prospect.get("bio"),
        recent_captions=recent_captions,
        follower_count=prospect.get("follower_count"),
    )

    template = template_for(profile.vibe)

    human_payload = {
        "vibe_profile": profile.to_payload(),
        "comment_template": template,
        "target_post_url": target_post_url,
        "prospect_handle": prospect["handle"],
        "prospect_bio": prospect.get("bio", "")[:300],
    }

    repo.create_warming_action(
        prospect_id=prospect["id"],
        ig_account=ig_account,
        action="comment",
        target_url=target_post_url,
        scheduled_for=_pick_scheduled_time(),
        status="skipped_human_queue",   # comments are always HiTL per Mason
        human_payload=human_payload,
    )
```

## Discord queue surface

When the Discord bot posts a comment action to the human queue, render the
vibe hint prominently so the operator knows the style before they click into
the prospect:

```
🗨️  COMMENT NEEDED — @somecoach
Vibe: professional (conf 0.78) — Thoughtful, substantive compliment
Style:
  • full sentence, proper capitalization
  • zero emojis (or 1 max if the post itself uses one)
  • reference the SPECIFIC idea/point they made
  • 10-20 words

Starters:
  • "The point about [SPECIFIC IDEA] hits hard — most people miss exactly that."
  • "Surprisingly few people actually do step 3 — appreciate the honesty here."
  • "This reframe is the part most courses skip over."

Target post: https://instagram.com/p/...
```

## Env vars

```bash
# M5 vibe classifier (additions)
M5_VIBE_USE_LLM=false                    # default OFF — heuristic is enough for V1
M5_VIBE_LLM_FALLBACK_THRESHOLD=0.60      # below this, fall back to LLM if enabled
M5_VIBE_MODEL=claude-haiku-4-5-20251001  # cheap fallback model
```

## What the classifier does NOT do

- It does NOT auto-post comments. Mason's spec keeps comments human-in-loop;
  the classifier only suggests the *style*, the human writes the actual text.
- It does NOT classify based on profile picture, follower-to-following ratio,
  story content, or video. Only text in bio + caption history.
- It does NOT learn from outcomes. V2 could track which vibe → which
  conversion rate and tune the thresholds; for now it's pure heuristic.

## Cost profile

Heuristic phase (default): **zero cost, ~0.5 ms per prospect**.

LLM fallback (opt-in, only for ambiguous cases — typically ~20-30% of prospects):
~$0.001 per prospect on Claude Haiku 4.5. Even at 1,000 prospects/day, that's
~$0.30/day.

## When to revisit

After the first ~50 commented prospects, audit the queue: did the human use
the suggested style? If the human consistently overrides the classifier on a
specific niche (e.g., "fitness coaches keep getting classified as casual but
they're educational creators in disguise"), retune the keyword sets or flip
on `M5_VIBE_USE_LLM=true` for better accuracy.
