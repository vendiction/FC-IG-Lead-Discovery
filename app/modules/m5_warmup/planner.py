"""
M5 Warm-up Planner.

For a qualified prospect with an opener ready, plan a warm-up sequence:
1. follow         — t = now + small random offset
2. like_post #1   — t = follow + 5-15 min
3. like_post #2   — t = like_post #1 + 10-30 min
4. comment        — queued to Discord for human; no auto-execute in V1
5. story_view     — skipped in V1 (added in V2 with reels_media API)

Resulting rows go into warming_actions with status='scheduled'.
The executor will pick them up when scheduled_for <= now.

After all non-comment actions complete, the prospect moves to warmup_complete
with dm_unblock_at = last_action + 24h (Mason's gap before DM send).

Change log (2026-06-23):
  Wired the vibe classifier into the comment action's human_payload so the
  Discord queue tells the human "fire emojis vs professional compliment"
  per-prospect. Pure additive change — falls back to the old generic payload
  if classifier raises for any reason.
"""
from __future__ import annotations
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from app.core.supabase_client import get_supabase
from app.core.logging import get_logger

# Vibe classifier (shipped in this module today). Soft-import so the planner
# still functions even if anyone deletes the vibe files — comments just fall
# back to the generic instructions block.
try:
    from .vibe_classifier import classify_vibe
    from .comment_suggestions import template_for
    _VIBE_AVAILABLE = True
except Exception as _e:  # pragma: no cover
    _VIBE_AVAILABLE = False

log = get_logger("m5.planner")


# ────────────────────────────────────────────────────────────────────
# Timing config
# ────────────────────────────────────────────────────────────────────

# Spacing between consecutive actions on the SAME prospect.
# Mason's spec: "warm-up over hours, not seconds"
FOLLOW_DELAY_MIN_S = 30           # at least 30s after planning
FOLLOW_DELAY_MAX_S = 180          # up to 3 min jitter

LIKE_1_DELAY_AFTER_FOLLOW_MIN_S = 5 * 60        # 5 min
LIKE_1_DELAY_AFTER_FOLLOW_MAX_S = 15 * 60       # 15 min

LIKE_2_DELAY_AFTER_LIKE_1_MIN_S = 10 * 60       # 10 min
LIKE_2_DELAY_AFTER_LIKE_1_MAX_S = 30 * 60       # 30 min

COMMENT_DELAY_AFTER_LIKE_2_MIN_S = 30 * 60      # 30 min
COMMENT_DELAY_AFTER_LIKE_2_MAX_S = 90 * 60      # 90 min

# Story-action timing (Mason: "occasional swiping interactions ... heart story
# updates"). Stories slot between like #1 and like #2 — natural pacing, and
# stories often expire within hours so we don't want them at the very end of
# the warm-up sequence where they'd be gone by execution time.
STORY_VIEW_DELAY_AFTER_LIKE_1_MIN_S = 2 * 60     # 2 min after first like
STORY_VIEW_DELAY_AFTER_LIKE_1_MAX_S = 8 * 60     # up to 8 min
STORY_LIKE_DELAY_AFTER_STORY_VIEW_MIN_S = 30     # 30 s after view
STORY_LIKE_DELAY_AFTER_STORY_VIEW_MAX_S = 120    # 2 min

# Probabilistic story actions — "occasional" per Mason's spec.
# Story view ≈ swipe. Story like ≈ heart. ~half of warm-ups include a swipe,
# and within those ~half also heart it. Net: ~25% of prospects get the heart.
STORY_VIEW_PROBABILITY = 0.50
STORY_LIKE_GIVEN_VIEW_PROBABILITY = 0.50

# Gap between last warm-up action and unblocking DM send
DM_GAP_HOURS = 24


def _jitter(now: datetime, min_s: int, max_s: int) -> datetime:
    return now + timedelta(seconds=random.randint(min_s, max_s))


# ────────────────────────────────────────────────────────────────────
# DB queries
# ────────────────────────────────────────────────────────────────────

def pick_prospects_to_plan(limit: int = 5, ig_account: str = "ignorethisdump2") -> list[dict]:
    """
    Qualified prospects:
    - status = 'pending_warmup'
    - have at least one opener
    - don't yet have any warming_actions rows scheduled for them
    """
    sb = get_supabase()

    have_actions = (sb.table("warming_actions")
                    .select("prospect_id")
                    .execute()).data or []
    excluded = list({r["prospect_id"] for r in have_actions if r.get("prospect_id")})

    have_opener = (sb.table("openers")
                   .select("prospect_id")
                   .execute()).data or []
    has_opener_ids = list({r["prospect_id"] for r in have_opener if r.get("prospect_id")})

    if not has_opener_ids:
        return []

    q = (sb.table("qualified_prospects")
         .select("id,account_id,handle,is_high_value")
         .eq("status", "pending_warmup")
         .in_("id", has_opener_ids))
    if excluded:
        q = q.not_.in_("id", excluded)
    prospects = (q.limit(limit).execute()).data or []
    return prospects


def _fetch_account_for_vibe(account_id: str) -> dict:
    """Pull bio + follower_count so the classifier has something to chew on."""
    sb = get_supabase()
    rows = (sb.table("accounts")
            .select("bio,follower_count")
            .eq("id", account_id).limit(1).execute()).data or []
    return rows[0] if rows else {}


def _build_follow_payload(prospect: dict) -> dict:
    """Operator card payload for the follow action.

    Operator just taps Follow on the prospect's profile. The card needs
    enough context for them to feel like a real human approving each
    target — score, niche signal, cross-platform discovery hint.
    """
    return {
        "handle": prospect["handle"],
        "score": prospect.get("score"),
        "follower_count": prospect.get("follower_count"),
        "primary_gap": prospect.get("primary_gap"),
        "cross_platform_discovery_source":
            prospect.get("cross_platform_discovery_source"),
        "instructions": "Open the profile and tap Follow.",
    }


def _build_like_payload(prospect: dict, like_number: int) -> dict:
    """Operator card payload for a like_post action.

    `like_number` is 1 or 2 — matches the round-robin media picker the
    executor used. The card tells the operator "this is like #1 of 2:
    open her most recent post and like it" so it stays human-paced.
    """
    return {
        "handle": prospect["handle"],
        "like_number": like_number,
        "total_likes_in_warmup": 2,
        "instructions": (
            f"This is like #{like_number} of 2. Open the prospect's profile, "
            f"tap into post #{like_number} (most recent if #1, second-most "
            f"if #2), and tap the heart. Read the caption first — that's the "
            f"social-history signal Mason cares about."
        ),
    }


def _build_story_payload(prospect: dict, action: str) -> dict:
    """Operator card payload for story_view or story_like.

    Stories expire in 24h, so by the time the operator opens the card the
    prospect may have nothing live. The card explicitly says: skip via
    `/story_skip` if there's no story to act on.
    """
    if action == "view":
        verb = "Swipe through her stories."
    else:
        verb = "Heart one of her stories (any of them — pick whatever lands)."
    return {
        "handle": prospect["handle"],
        "action_type": action,
        "instructions": (
            f"{verb} If she has no active stories right now, run /story_skip "
            f"and the system will mark it cancelled cleanly."
        ),
    }


def _build_comment_human_payload(prospect: dict) -> dict:
    """
    Run the vibe classifier on the prospect's bio (and follower_count)
    and return the payload the Discord queue will surface to the human.

    Returns the generic fallback payload if the classifier is unavailable
    or raises for any reason.
    """
    generic = {
        "handle": prospect["handle"],
        "instructions": (
            "Write a single vibe-matched comment on this prospect's most recent post. "
            "Reference something specific (a phrase, image, or topic from the post). "
            "Keep it under 15 words. Sound like a curious peer, not a fan or a salesperson. "
            "Do NOT mention your business or services."
        ),
    }
    if not _VIBE_AVAILABLE:
        return generic

    try:
        acct = _fetch_account_for_vibe(prospect["account_id"])
        profile = classify_vibe(
            bio=acct.get("bio"),
            recent_captions=[],   # captions not currently scraped — bio-only for V1
            follower_count=acct.get("follower_count"),
        )
        template = template_for(profile.vibe)

        log.info(
            "m5.plan.vibe_classified",
            handle=prospect["handle"],
            vibe=profile.vibe,
            confidence=round(profile.confidence, 3),
            method=profile.method,
        )

        return {
            **generic,
            "vibe_profile": profile.to_payload(),
            "comment_template": template,
        }
    except Exception as e:
        log.warning(
            "m5.plan.vibe_classifier_failed_falling_back",
            handle=prospect.get("handle"),
            err=str(e),
        )
        return generic


def decide_story_actions(rng: random.Random) -> tuple[bool, bool]:
    """Pure: should this prospect get a story_view + story_like in their plan?

    Returns (should_view, should_like). `should_like` implies `should_view`
    (you can't like a story without seeing it first). Both False is "no
    story actions for this prospect."

    Probability flow:
      view  = (rng < STORY_VIEW_PROBABILITY)
      like  = view AND (rng2 < STORY_LIKE_GIVEN_VIEW_PROBABILITY)

    Takes an explicit Random so tests can pin the seed.
    """
    should_view = rng.random() < STORY_VIEW_PROBABILITY
    if not should_view:
        return False, False
    should_like = rng.random() < STORY_LIKE_GIVEN_VIEW_PROBABILITY
    return True, should_like


# ────────────────────────────────────────────────────────────────────
# Plan one prospect
# ────────────────────────────────────────────────────────────────────

def plan_for_prospect(prospect: dict, ig_account: str = "ignorethisdump2") -> int:
    """
    Insert warming_actions rows for this prospect. Returns count inserted.
    """
    sb = get_supabase()
    now = datetime.now(timezone.utc)
    target_url = f"https://www.instagram.com/{prospect['handle']}/"

    rows: list[dict] = []

    # ─────────────────────────────────────────────────────────
    # Operator-mode warmup planning (2026-06-24).
    # Every IG write action goes into the Discord queue with
    # status='skipped_human_queue'. The executor (worker_warm)
    # is a no-op in this mode — actions get completed by the
    # operator's slash commands, not Playwright. This eliminates
    # the soft-block / shadow-ban risk that killed our earlier
    # automated warmup runs against ignorethisdump2.
    # ─────────────────────────────────────────────────────────
    OPERATOR_STATUS = "skipped_human_queue"

    # 1. follow
    t_follow = _jitter(now, FOLLOW_DELAY_MIN_S, FOLLOW_DELAY_MAX_S)
    rows.append({
        "prospect_id": prospect["id"],
        "ig_account": ig_account,
        "action": "follow",
        "target_url": target_url,
        "scheduled_for": t_follow.isoformat(),
        "status": OPERATOR_STATUS,
        "human_payload": _build_follow_payload(prospect),
    })

    # 2. like_post #1
    t_like1 = _jitter(t_follow, LIKE_1_DELAY_AFTER_FOLLOW_MIN_S, LIKE_1_DELAY_AFTER_FOLLOW_MAX_S)
    rows.append({
        "prospect_id": prospect["id"],
        "ig_account": ig_account,
        "action": "like_post",
        "target_url": target_url,
        "scheduled_for": t_like1.isoformat(),
        "status": OPERATOR_STATUS,
        "human_payload": _build_like_payload(prospect, like_number=1),
    })

    # 2.5. story_view + (sometimes) story_like — Mason's "swipe + heart"
    # Probabilistic so it doesn't look mechanical. Operator may skip if the
    # prospect has no active stories at execution time.
    should_view_story, should_like_story = decide_story_actions(random)
    last_t = t_like1
    if should_view_story:
        t_story_view = _jitter(
            t_like1,
            STORY_VIEW_DELAY_AFTER_LIKE_1_MIN_S,
            STORY_VIEW_DELAY_AFTER_LIKE_1_MAX_S,
        )
        rows.append({
            "prospect_id": prospect["id"],
            "ig_account": ig_account,
            "action": "story_view",
            "target_url": target_url,
            "scheduled_for": t_story_view.isoformat(),
            "status": OPERATOR_STATUS,
            "human_payload": _build_story_payload(prospect, action="view"),
        })
        last_t = t_story_view
        if should_like_story:
            t_story_like = _jitter(
                t_story_view,
                STORY_LIKE_DELAY_AFTER_STORY_VIEW_MIN_S,
                STORY_LIKE_DELAY_AFTER_STORY_VIEW_MAX_S,
            )
            rows.append({
                "prospect_id": prospect["id"],
                "ig_account": ig_account,
                "action": "story_like",
                "target_url": target_url,
                "scheduled_for": t_story_like.isoformat(),
                "status": OPERATOR_STATUS,
                "human_payload": _build_story_payload(prospect, action="like"),
            })
            last_t = t_story_like

    # 3. like_post #2
    t_like2 = _jitter(last_t, LIKE_2_DELAY_AFTER_LIKE_1_MIN_S, LIKE_2_DELAY_AFTER_LIKE_1_MAX_S)
    rows.append({
        "prospect_id": prospect["id"],
        "ig_account": ig_account,
        "action": "like_post",
        "target_url": target_url,
        "scheduled_for": t_like2.isoformat(),
        "status": OPERATOR_STATUS,
        "human_payload": _build_like_payload(prospect, like_number=2),
    })

    # 4. comment — vibe-classified payload (already operator-queued pre-change)
    t_comment = _jitter(t_like2, COMMENT_DELAY_AFTER_LIKE_2_MIN_S, COMMENT_DELAY_AFTER_LIKE_2_MAX_S)
    rows.append({
        "prospect_id": prospect["id"],
        "ig_account": ig_account,
        "action": "comment",
        "target_url": target_url,
        "scheduled_for": t_comment.isoformat(),
        "status": OPERATOR_STATUS,
        "human_payload": _build_comment_human_payload(prospect),
    })

    # Insert
    res = sb.table("warming_actions").insert(rows).execute()
    inserted = len(res.data or [])

    log.info("m5.plan.created",
             handle=prospect["handle"],
             prospect_id=prospect["id"],
             ig_account=ig_account,
             actions_scheduled=inserted,
             story_view=should_view_story,
             story_like=should_like_story,
             first_action_at=t_follow.isoformat(),
             last_action_at=t_comment.isoformat())
    return inserted


# ────────────────────────────────────────────────────────────────────
# Public entry: plan all eligible prospects
# ────────────────────────────────────────────────────────────────────

def plan_batch(limit: int = 5, ig_account: str = "ignorethisdump2") -> int:
    """Plan warm-up for up to `limit` eligible prospects. Returns total actions inserted."""
    prospects = pick_prospects_to_plan(limit=limit, ig_account=ig_account)
    if not prospects:
        return 0
    total = 0
    for p in prospects:
        try:
            total += plan_for_prospect(p, ig_account=ig_account)
        except Exception as e:
            log.error("m5.plan.failed", handle=p.get("handle"), err=str(e), exc_info=True)
    return total
