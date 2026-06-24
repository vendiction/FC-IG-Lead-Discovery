"""
M8 — Discord bot for human-in-the-loop handoffs.

Responsibilities:
  1. Poll `handoffs` table for new rows (status='pending') and post each as
     an embed in the configured Discord channel.
  2. Provide three slash commands for the human operator:
       /claim <handoff_id>   — assign yourself
       /resolve <handoff_id> <notes>  — close out, mark resolved
       /return <handoff_id>  — return control to the AI agent

The bot does NOT itself send DMs — that's M7. When a handoff is /returned,
the conversation's human_intervention flag is cleared and stage is restored
to whatever it was BEFORE handoff (we store that in trigger_detail JSON if
needed; for V1 we restore to 'escalation' as a safe default).

Required env vars:
  DISCORD_BOT_TOKEN
  DISCORD_HANDOFF_CHANNEL_ID    # integer
"""
from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional
import structlog
import discord
from discord import app_commands
from discord.ext import commands, tasks

from app.core.logging import configure_logging  # type: ignore
from app.core.supabase_client import get_supabase  # type: ignore

configure_logging()
log = structlog.get_logger("m8.discord")


TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("DISCORD_HANDOFF_CHANNEL_ID", "0"))
# Comment queue can live in its own channel for noise control; falls back to
# the handoff channel if not set.
WARMING_CHANNEL_ID = int(
    os.environ.get("DISCORD_WARMING_QUEUE_CHANNEL_ID", str(CHANNEL_ID))
)
POLL_INTERVAL_SECONDS = int(os.getenv("M8_DISCORD_POLL_SECONDS", "30"))
WARMING_POLL_INTERVAL_SECONDS = int(os.getenv("M8_DISCORD_WARMING_POLL_SECONDS", "60"))


# ────────────────────────────────────────────────────────────────────
# Bot setup
# ────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    log.info("m8.discord.ready", user=str(bot.user))
    # Guild-scoped sync makes new commands appear in autocomplete instantly,
    # within the configured server. Global sync (the alternative) takes up to
    # an hour to propagate. For single-server bots, always prefer guild sync.
    # Set DISCORD_GUILD_ID in .env to enable.
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    try:
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            # Copy global tree to the guild scope, then sync that guild.
            # This is the discord.py-idiomatic way to push commands to one server.
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info("m8.discord.commands_synced",
                     count=len(synced), scope="guild", guild_id=guild_id)
        else:
            synced = await bot.tree.sync()
            log.info("m8.discord.commands_synced",
                     count=len(synced), scope="global",
                     note="set DISCORD_GUILD_ID for instant propagation")
    except Exception as e:
        log.error("m8.discord.command_sync_failed", err=str(e))
    poll_handoffs.start()
    poll_warming_comments.start()
    # Operator-mode pollers (everything routes through Discord)
    poll_warming_actions.start()
    poll_dm_sends.start()
    poll_pending_replies.start()
    poll_ghost_followups.start()


# ────────────────────────────────────────────────────────────────────
# Periodic handoff poll → post new ones
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def poll_handoffs():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        log.warning("m8.discord.channel_not_found", channel_id=CHANNEL_ID)
        return

    sb = get_supabase()
    # New handoffs = pending and no discord_message_id yet
    rows = (sb.table("handoffs").select("*")
            .eq("status", "pending")
            .is_("discord_message_id", "null")
            .order("created_at", desc=False)
            .limit(20)
            .execute()).data or []

    for h in rows:
        embed = _build_handoff_embed(h)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error("m8.discord.post_failed", handoff_id=h["id"], err=str(e))
            continue

        sb.table("handoffs").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", h["id"]).execute()

        log.info(
            "m8.discord.handoff_posted",
            handoff_id=h["id"],
            discord_msg_id=str(msg.id),
            trigger=h["trigger_reason"],
        )


def _build_handoff_embed(h: dict) -> discord.Embed:
    """Render a handoff as a Discord embed with full context."""
    sb = get_supabase()
    # Look up conversation and prospect for richer display
    conv = (sb.table("conversations").select("*")
            .eq("id", h["conversation_id"]).single().execute()).data
    prospect = (sb.table("qualified_prospects").select("*")
                .eq("id", h["prospect_id"]).single().execute()).data
    ig_account = conv.get("ig_account") if conv else "?"

    color = {
        "high_value": discord.Color.gold(),
        "low_confidence": discord.Color.orange(),
        "nuance_required": discord.Color.purple(),
        "user_requested": discord.Color.blue(),
        "objection_escalation": discord.Color.red(),
    }.get(h["trigger_reason"], discord.Color.greyple())

    embed = discord.Embed(
        title=f"🚦 Handoff: @{prospect.get('handle', '?')}",
        description=(
            f"**Reason**: `{h['trigger_reason']}`\n"
            f"**Detail**: {h.get('trigger_detail') or '_none_'}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="IG account", value=f"@{ig_account}", inline=True)
    embed.add_field(name="Score", value=str(prospect.get("total_score", "?")), inline=True)
    embed.add_field(name="Stage", value=conv.get("current_stage", "?") if conv else "?", inline=True)

    # Last 6 messages from snapshot
    snap = h.get("conversation_snapshot") or []
    if isinstance(snap, str):
        try:
            snap = json.loads(snap)
        except Exception:
            snap = []
    if snap:
        transcript_lines = []
        for m in snap[-6:]:
            spk = "→ AGENT" if m.get("direction") == "outbound" else "← PROSPECT"
            body = (m.get("body") or "")[:200]
            transcript_lines.append(f"`{spk}` {body}")
        embed.add_field(
            name="Recent transcript",
            value="\n".join(transcript_lines) or "_empty_",
            inline=False,
        )

    if h.get("ai_recommended_action"):
        embed.add_field(
            name="🤖 AI drafted",
            value=h["ai_recommended_action"][:1000],
            inline=False,
        )

    embed.set_footer(text=f"handoff_id: {h['id']}")
    return embed


# ────────────────────────────────────────────────────────────────────
# Periodic warming-comment poll → post new ones
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=WARMING_POLL_INTERVAL_SECONDS)
async def poll_warming_comments():
    """
    Surfaces M5 vibe-classified comment tasks to the human queue.

    Picks `warming_actions` rows where action='comment',
    status='skipped_human_queue', scheduled_for <= now, and not yet posted
    to discord. Renders an embed with the vibe profile + comment template,
    then marks the row with discord_message_id so it isn't re-posted.
    """
    channel = bot.get_channel(WARMING_CHANNEL_ID)
    if channel is None:
        log.warning(
            "m8.discord.warming_channel_not_found",
            channel_id=WARMING_CHANNEL_ID,
        )
        return

    sb = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = (
        sb.table("warming_actions").select("*")
        .eq("action", "comment")
        .eq("status", "skipped_human_queue")
        .is_("discord_message_id", "null")
        .lte("scheduled_for", now_iso)
        .order("scheduled_for", desc=False)
        .limit(20)
        .execute()
    ).data or []

    for w in rows:
        embed = _build_warming_comment_embed(w)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error(
                "m8.discord.warming_post_failed",
                warming_id=w["id"], err=str(e),
            )
            continue

        sb.table("warming_actions").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", w["id"]).execute()

        log.info(
            "m8.discord.warming_comment_posted",
            warming_id=w["id"],
            discord_msg_id=str(msg.id),
        )


def _build_warming_comment_embed(w: dict) -> discord.Embed:
    """Render a warming-action comment task as a Discord embed."""
    payload = w.get("human_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    vibe_block = payload.get("vibe_profile") or {}
    template = payload.get("comment_template") or {}
    vibe = vibe_block.get("vibe", "unknown")

    color_by_vibe = {
        "casual":       discord.Color.from_rgb(255, 100, 60),
        "professional": discord.Color.from_rgb(60, 120, 200),
        "mixed":        discord.Color.from_rgb(140, 100, 200),
        "unknown":      discord.Color.greyple(),
    }
    color = color_by_vibe.get(vibe, discord.Color.greyple())

    handle = payload.get("handle", "?")
    embed = discord.Embed(
        title=f"💬 Comment task: @{handle}",
        description=(
            f"**Vibe**: `{vibe}` "
            f"(conf {vibe_block.get('confidence', '?')}, "
            f"method `{vibe_block.get('method', '?')}`)\n"
            f"**Target post**: {w.get('target_url') or '_no URL_'}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    if vibe_block.get("suggested_comment_style"):
        embed.add_field(
            name="Style hint",
            value=vibe_block["suggested_comment_style"][:1000],
            inline=False,
        )

    if template.get("style_label"):
        embed.add_field(
            name=f"Template — {template['style_label']}",
            value=(
                "**Rules:**\n• "
                + "\n• ".join((template.get("rules") or [])[:6])
                + "\n\n**Starters:**\n• "
                + "\n• ".join((template.get("example_starters") or [])[:5])
            )[:1000],
            inline=False,
        )

    if payload.get("instructions"):
        embed.add_field(
            name="Instructions",
            value=payload["instructions"][:1000],
            inline=False,
        )

    embed.set_footer(
        text=(
            f"warming_id: {w['id']} · "
            f"to mark done: /comment_done {w['id']} <text-you-posted>"
        )
    )
    return embed


# ────────────────────────────────────────────────────────────────────
# Slash commands
# ────────────────────────────────────────────────────────────────────

@bot.tree.command(name="claim", description="Claim a handoff by ID")
@app_commands.describe(handoff_id="UUID of the handoff to claim")
async def claim(interaction: discord.Interaction, handoff_id: str):
    sb = get_supabase()
    r = (sb.table("handoffs").update({
        "status": "claimed",
        "assigned_to": str(interaction.user.id),
    }).eq("id", handoff_id).eq("status", "pending").execute())
    if not r.data:
        await interaction.response.send_message(
            f"Could not claim `{handoff_id}` — already claimed, resolved, or doesn't exist.",
            ephemeral=True,
        )
        return
    log.info("m8.discord.claimed", handoff_id=handoff_id, user_id=str(interaction.user.id))
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} claimed handoff `{handoff_id}`.",
    )


@bot.tree.command(name="resolve", description="Mark a handoff resolved with notes")
@app_commands.describe(
    handoff_id="UUID of the handoff",
    notes="What happened — for the audit log",
)
async def resolve(interaction: discord.Interaction, handoff_id: str, notes: str):
    sb = get_supabase()
    r = (sb.table("handoffs").update({
        "status": "resolved",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolution_notes": notes,
    }).eq("id", handoff_id).execute())
    if not r.data:
        await interaction.response.send_message(
            f"No handoff `{handoff_id}` found.", ephemeral=True,
        )
        return
    log.info("m8.discord.resolved", handoff_id=handoff_id, user_id=str(interaction.user.id))
    await interaction.response.send_message(
        f"✅ Resolved `{handoff_id}` — {notes[:200]}",
    )


@bot.tree.command(name="return", description="Return a handoff to the AI agent")
@app_commands.describe(
    handoff_id="UUID of the handoff",
    resume_stage="Stage to resume at: escalation | invitation | action",
)
async def return_to_ai(
    interaction: discord.Interaction,
    handoff_id: str,
    resume_stage: str = "escalation",
):
    if resume_stage not in ("opener", "escalation", "invitation", "action"):
        await interaction.response.send_message(
            "resume_stage must be one of: opener, escalation, invitation, action",
            ephemeral=True,
        )
        return

    sb = get_supabase()
    handoff = (sb.table("handoffs").select("conversation_id")
               .eq("id", handoff_id).single().execute()).data
    if not handoff:
        await interaction.response.send_message(
            f"No handoff `{handoff_id}` found.", ephemeral=True,
        )
        return

    # Restore conversation state
    sb.table("conversations").update({
        "current_stage": resume_stage,
        "human_intervention": False,
        "stage_entered_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", handoff["conversation_id"]).execute()

    sb.table("handoffs").update({
        "status": "returned_to_ai",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "resolution_notes": f"returned to AI at stage={resume_stage} by {interaction.user.name}",
    }).eq("id", handoff_id).execute()

    log.info(
        "m8.discord.returned_to_ai",
        handoff_id=handoff_id, resume_stage=resume_stage,
    )
    await interaction.response.send_message(
        f"🔄 Returned `{handoff_id}` to AI at stage `{resume_stage}`.",
    )


@bot.tree.command(name="queue", description="Show pending handoffs")
async def queue(interaction: discord.Interaction):
    sb = get_supabase()
    rows = (sb.table("handoffs").select("id,trigger_reason,prospect_id,created_at")
            .eq("status", "pending")
            .order("created_at", desc=False)
            .limit(15)
            .execute()).data or []

    if not rows:
        await interaction.response.send_message("📭 No pending handoffs.", ephemeral=True)
        return

    lines = []
    for r in rows:
        prospect = (sb.table("qualified_prospects").select("handle")
                    .eq("id", r["prospect_id"]).single().execute()).data
        lines.append(
            f"`{r['id'][:8]}` @{prospect.get('handle', '?')} — "
            f"`{r['trigger_reason']}` ({r['created_at'][:16]})"
        )
    await interaction.response.send_message(
        "**Pending handoffs:**\n" + "\n".join(lines),
        ephemeral=True,
    )


# ────────────────────────────────────────────────────────────────────
# Warming-comment slash commands
# ────────────────────────────────────────────────────────────────────

@bot.tree.command(
    name="comment_done",
    description="Mark a warming-comment task done after posting on IG",
)
@app_commands.describe(
    warming_id="UUID of the warming_action row (from the embed footer)",
    posted_text="The actual comment text you posted (for audit)",
)
async def comment_done(
    interaction: discord.Interaction, warming_id: str, posted_text: str,
):
    sb = get_supabase()
    r = (sb.table("warming_actions").update({
        "status": "human_completed",
        "human_assignee": str(interaction.user.id),
        "human_response_text": posted_text[:2000],
        "human_completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", warming_id).eq("status", "skipped_human_queue").execute())

    if not r.data:
        await interaction.response.send_message(
            f"Could not mark `{warming_id}` done — already completed, "
            f"cancelled, or doesn't exist.",
            ephemeral=True,
        )
        return

    log.info(
        "m8.discord.warming_comment_done",
        warming_id=warming_id,
        user_id=str(interaction.user.id),
    )
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} completed comment task "
        f"`{warming_id[:8]}`.",
    )


@bot.tree.command(
    name="comment_skip",
    description="Skip a warming-comment task (profile off-niche, deleted post, etc.)",
)
@app_commands.describe(
    warming_id="UUID of the warming_action row",
    reason="Why you're skipping it (for audit)",
)
async def comment_skip(
    interaction: discord.Interaction, warming_id: str, reason: str,
):
    sb = get_supabase()
    r = (sb.table("warming_actions").update({
        "status": "cancelled",
        "human_assignee": str(interaction.user.id),
        "failure_reason": f"human_skipped: {reason[:400]}",
        "human_completed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", warming_id).eq("status", "skipped_human_queue").execute())

    if not r.data:
        await interaction.response.send_message(
            f"Could not skip `{warming_id}` — already resolved or doesn't exist.",
            ephemeral=True,
        )
        return

    log.info("m8.discord.warming_comment_skipped",
             warming_id=warming_id, reason=reason[:200])
    await interaction.response.send_message(
        f"⏭️ Skipped `{warming_id[:8]}` — {reason[:200]}",
    )


@bot.tree.command(
    name="comment_queue",
    description="Show pending warming-comment tasks",
)
async def comment_queue(interaction: discord.Interaction):
    sb = get_supabase()
    rows = (sb.table("warming_actions")
            .select("id,scheduled_for,human_payload,target_url")
            .eq("action", "comment")
            .eq("status", "skipped_human_queue")
            .order("scheduled_for", desc=False)
            .limit(15)
            .execute()).data or []

    if not rows:
        await interaction.response.send_message(
            "📭 No pending comment tasks.", ephemeral=True,
        )
        return

    lines = []
    for r in rows:
        payload = r.get("human_payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        handle = payload.get("handle", "?")
        vibe = (payload.get("vibe_profile") or {}).get("vibe", "?")
        lines.append(
            f"`{r['id'][:8]}` @{handle} — `{vibe}` "
            f"({(r.get('scheduled_for') or '')[:16]})"
        )
    await interaction.response.send_message(
        "**Pending comment tasks:**\n" + "\n".join(lines),
        ephemeral=True,
    )


# ════════════════════════════════════════════════════════════════════
# OPERATOR MODE — Every IG write action routes through Discord
# ════════════════════════════════════════════════════════════════════
# Each section below has the same shape:
#   1. A @tasks.loop poller that picks rows due-for-operator and posts
#      one Discord embed per row
#   2. An embed builder that renders the prospect context + clear
#      copy-pasteable text / tap targets
#   3. Slash commands that the operator runs to mark each card done
#
# All embed builders share these design rules:
#   - Title prefixed by an emoji that visually distinguishes the card type
#   - One link to instagram.com that's tappable on mobile
#   - Copy-pasteable text in triple-backtick blocks (mobile long-press copy)
#   - The card UUID printed in a footer so /<cmd>_done <id> is easy to type
# ════════════════════════════════════════════════════════════════════


def _profile_url(handle: str) -> str:
    return f"https://www.instagram.com/{handle.lstrip('@')}/"


# ────────────────────────────────────────────────────────────────────
# A. WARMUP: follow + like + story actions
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=WARMING_POLL_INTERVAL_SECONDS)
async def poll_warming_actions():
    """Pick up follow / like_post / story_view / story_like rows that
    the operator should execute on their phone. Comment actions have
    their own poller (poll_warming_comments) — keeping them separate
    because comments need vibe-classified payload rendering."""
    channel = bot.get_channel(WARMING_CHANNEL_ID)
    if channel is None:
        return

    sb = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = (
        sb.table("warming_actions").select("*")
        .in_("action", ["follow", "like_post", "story_view", "story_like"])
        .eq("status", "skipped_human_queue")
        .is_("discord_message_id", "null")
        .lte("scheduled_for", now_iso)
        .order("scheduled_for", desc=False)
        .limit(20)
        .execute()
    ).data or []

    for w in rows:
        embed = _build_warmup_action_embed(w)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error("m8.discord.warmup_post_failed",
                      action_id=w["id"], err=str(e))
            continue

        sb.table("warming_actions").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", w["id"]).execute()

        log.info("m8.discord.warmup_action_posted",
                 action_id=w["id"], action=w["action"])


def _build_warmup_action_embed(w: dict) -> discord.Embed:
    """Render a follow/like/story action as a tappable card."""
    action = w["action"]
    payload = w.get("human_payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}

    handle = payload.get("handle", "unknown")

    titles = {
        "follow": ("🟢 WARMUP: Follow", discord.Color.green()),
        "like_post": ("💛 WARMUP: Like a post", discord.Color.gold()),
        "story_view": ("👀 WARMUP: View stories", discord.Color.blurple()),
        "story_like": ("❤️ WARMUP: Heart a story", discord.Color.red()),
    }
    title, color = titles.get(action, ("WARMUP", discord.Color.dark_grey()))

    embed = discord.Embed(
        title=title,
        description=f"**@{handle}**",
        color=color,
    )

    # Context block — different details per action
    if action == "follow":
        ctx_lines = []
        if payload.get("score") is not None:
            ctx_lines.append(f"Score: **{payload['score']}/100**")
        if payload.get("follower_count"):
            ctx_lines.append(f"Followers: **{payload['follower_count']:,}**")
        if payload.get("primary_gap"):
            ctx_lines.append(f"Gap: `{payload['primary_gap']}`")
        if payload.get("cross_platform_discovery_source"):
            ctx_lines.append(
                f"Bigger on: **{payload['cross_platform_discovery_source']}**"
            )
        if ctx_lines:
            embed.add_field(name="Why she matters",
                            value="\n".join(ctx_lines), inline=False)

    elif action == "like_post":
        n = payload.get("like_number", 1)
        embed.add_field(
            name="Which post",
            value=(
                f"This is like **#{n} of 2**. "
                f"Tap into post #{n} on her profile and like it."
            ),
            inline=False,
        )

    elif action in ("story_view", "story_like"):
        embed.add_field(
            name="Heads up",
            value=(
                "Stories expire — she may not have an active one. "
                f"Use `/story_skip {w['id']}` if there's nothing live."
            ),
            inline=False,
        )

    # Instructions
    embed.add_field(
        name="Action",
        value=payload.get("instructions", "Open the profile and act."),
        inline=False,
    )

    # Link
    embed.add_field(
        name="Open in IG",
        value=_profile_url(handle),
        inline=False,
    )

    # Slash command hints
    cmd_map = {
        "follow": ("/follow_done", "/follow_skip"),
        "like_post": ("/like_done", "/like_skip"),
        "story_view": ("/story_done", "/story_skip"),
        "story_like": ("/story_done", "/story_skip"),
    }
    done_cmd, skip_cmd = cmd_map.get(action, ("/follow_done", "/follow_skip"))
    embed.add_field(
        name="Mark complete",
        value=f"✅ `{done_cmd} {w['id']}`\n⏭️ `{skip_cmd} {w['id']} reason:<why>`",
        inline=False,
    )

    embed.set_footer(text=f"action_id: {w['id']}")
    return embed


# ────────────────────────────────────────────────────────────────────
# B. DM SEND: opener delivery
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=WARMING_POLL_INTERVAL_SECONDS)
async def poll_dm_sends():
    """Pick up approved openers awaiting send."""
    channel = bot.get_channel(WARMING_CHANNEL_ID)
    if channel is None:
        return

    sb = get_supabase()
    rows = (
        sb.table("openers").select("*")
        .eq("approved_for_send", True)
        .is_("sent_at", "null")
        .is_("discord_message_id", "null")
        .order("approved_at", desc=False)
        .limit(10)
        .execute()
    ).data or []

    for opener in rows:
        # Resolve prospect context for the embed.
        # qualified_prospects has total_score + handle, but follower_count
        # lives on accounts and primary_gap lives on gap_analysis. We
        # gather all three with defensive try/except so a single missing
        # field doesn't take down the poller (which is what happened in
        # the original implementation when columns were assumed wrong).
        prospect_ctx: dict = {}
        try:
            qp = (sb.table("qualified_prospects")
                  .select("handle,total_score,account_id,is_high_value")
                  .eq("id", opener["prospect_id"]).limit(1).execute()).data
            if qp:
                prospect_ctx["handle"] = qp[0].get("handle")
                prospect_ctx["score"] = qp[0].get("total_score")
                prospect_ctx["is_high_value"] = qp[0].get("is_high_value")
                acct_id = qp[0].get("account_id")
                if acct_id:
                    try:
                        acct = (sb.table("accounts")
                                .select("follower_count,bio,external_url")
                                .eq("id", acct_id).limit(1).execute()).data
                        if acct:
                            prospect_ctx["follower_count"] = acct[0].get("follower_count")
                            prospect_ctx["bio"] = acct[0].get("bio")
                    except Exception as e:
                        log.debug("m8.discord.dm_acct_lookup_skip",
                                  opener_id=opener["id"], err=str(e))
                    try:
                        gap = (sb.table("gap_analysis")
                               .select("primary_gap,cross_platform_discovery_source,gap_evidence")
                               .eq("account_id", acct_id).limit(1).execute()).data
                        if gap:
                            prospect_ctx["primary_gap"] = gap[0].get("primary_gap")
                            prospect_ctx["cross_platform_discovery_source"] = \
                                gap[0].get("cross_platform_discovery_source")
                            prospect_ctx["gap_evidence"] = gap[0].get("gap_evidence")
                    except Exception as e:
                        log.debug("m8.discord.dm_gap_lookup_skip",
                                  opener_id=opener["id"], err=str(e))
        except Exception as e:
            log.warning("m8.discord.dm_context_fetch_failed",
                        opener_id=opener["id"], err=str(e))

        embed = _build_dm_send_embed(opener, prospect_ctx)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error("m8.discord.dm_post_failed",
                      opener_id=opener["id"], err=str(e))
            continue

        sb.table("openers").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", opener["id"]).execute()

        log.info("m8.discord.dm_card_posted", opener_id=opener["id"])


def _build_dm_send_embed(opener: dict, prospect: dict) -> discord.Embed:
    handle = prospect.get("handle", "unknown")
    is_hv = prospect.get("is_high_value")

    embed = discord.Embed(
        title=("📨 SEND DM — opener" + (" · 🔥 HIGH VALUE" if is_hv else "")),
        description=f"**@{handle}**",
        color=discord.Color.purple(),
    )

    # Context block
    ctx_lines = []
    if prospect.get("score") is not None:
        ctx_lines.append(f"Score: **{prospect['score']}/100**")
    if prospect.get("follower_count"):
        ctx_lines.append(f"Followers: **{prospect['follower_count']:,}**")
    if prospect.get("primary_gap"):
        ctx_lines.append(f"Gap: `{prospect['primary_gap']}`")
    if prospect.get("cross_platform_discovery_source"):
        ctx_lines.append(
            f"Discovery angle: **{prospect['cross_platform_discovery_source']}**"
        )
    if ctx_lines:
        embed.add_field(name="Why this opener",
                        value="\n".join(ctx_lines), inline=False)

    if prospect.get("gap_evidence"):
        embed.add_field(
            name="Gap detail",
            value=str(prospect["gap_evidence"])[:200],
            inline=False,
        )

    # The actual opener text — copy-pasteable in a code block
    opener_text = opener.get("opener_text") or opener.get("text") or ""
    embed.add_field(
        name=f"Opener ({len(opener_text)} chars)",
        value=f"```\n{opener_text}\n```",
        inline=False,
    )

    # How-to
    embed.add_field(
        name="How to send",
        value=(
            f"1. Open {_profile_url(handle)}\n"
            "2. Tap **Message**\n"
            "3. Long-press → Paste\n"
            "4. Send"
        ),
        inline=False,
    )

    embed.add_field(
        name="Mark complete",
        value=(
            f"✅ `/dm_sent {opener['id']}`\n"
            f"⚠️ `/dm_issue {opener['id']} reason:<what happened>`"
        ),
        inline=False,
    )

    embed.set_footer(text=f"opener_id: {opener['id']}")
    return embed


# ────────────────────────────────────────────────────────────────────
# C. CONVERSATION REPLIES: M7-drafted, operator-sent
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=WARMING_POLL_INTERVAL_SECONDS)
async def poll_pending_replies():
    """Pick up AI-drafted replies awaiting operator paste-and-send."""
    channel = bot.get_channel(WARMING_CHANNEL_ID)
    if channel is None:
        return

    sb = get_supabase()
    rows = (
        sb.table("pending_outbound_messages").select("*")
        .eq("status", "awaiting_operator")
        .is_("discord_message_id", "null")
        .order("created_at", desc=False)
        .limit(10)
        .execute()
    ).data or []

    for p in rows:
        embed = _build_reply_embed(p)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error("m8.discord.reply_post_failed",
                      pending_id=p["id"], err=str(e))
            continue

        sb.table("pending_outbound_messages").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", p["id"]).execute()

        log.info("m8.discord.reply_card_posted", pending_id=p["id"])


def _build_reply_embed(p: dict) -> discord.Embed:
    handle = p.get("prospect_handle", "unknown")
    embed = discord.Embed(
        title="💬 INBOUND REPLY — draft ready",
        description=f"**@{handle}** · Stage: `{p.get('stage_at_decision', '?')}`",
        color=discord.Color.teal(),
    )

    # Conversation excerpt — last 2-3 messages from history_snapshot
    history = p.get("history_snapshot") or []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except Exception:
            history = []

    if history:
        tail = history[-3:] if len(history) > 3 else history
        transcript_lines = []
        for msg in tail:
            direction = msg.get("direction", "?").upper()
            body = (msg.get("body") or "")[:200]
            transcript_lines.append(f"**{direction}:** {body}")
        embed.add_field(
            name="Conversation",
            value="\n\n".join(transcript_lines)[:1024],
            inline=False,
        )

    # AI confidence
    conf = p.get("ai_confidence", 0)
    embed.add_field(
        name="AI confidence",
        value=f"{conf:.2f}",
        inline=True,
    )

    # The draft reply
    draft = p.get("message_text") or ""
    embed.add_field(
        name=f"AI-drafted reply ({len(draft)} chars)",
        value=f"```\n{draft}\n```",
        inline=False,
    )

    embed.add_field(
        name="How to send",
        value=(
            f"1. Open {_profile_url(handle)} → Message\n"
            "2. Long-press to paste\n"
            "3. Send (edit if it feels off)"
        ),
        inline=False,
    )

    embed.add_field(
        name="Mark complete",
        value=(
            f"✅ `/reply_sent {p['id']}` — sent as-drafted\n"
            f"✏️ `/reply_edit {p['id']} text:<your version>` — sent with edits\n"
            f"🚨 `/reply_escalate {p['id']}` — hand to you instead"
        ),
        inline=False,
    )

    embed.set_footer(text=f"pending_id: {p['id']}")
    return embed


# ────────────────────────────────────────────────────────────────────
# D. GHOST FOLLOWUPS
# ────────────────────────────────────────────────────────────────────

@tasks.loop(seconds=WARMING_POLL_INTERVAL_SECONDS)
async def poll_ghost_followups():
    """Pick up ghost followups marked ready_for_operator."""
    channel = bot.get_channel(WARMING_CHANNEL_ID)
    if channel is None:
        return

    sb = get_supabase()
    rows = (
        sb.table("followups").select("*")
        .eq("status", "ready_for_operator")
        .is_("discord_message_id", "null")
        .limit(10).execute()
    ).data or []

    for f in rows:
        # Resolve handle for the embed
        conv = (sb.table("conversations").select("prospect_id")
                .eq("id", f["conversation_id"]).limit(1).execute()).data
        if not conv:
            continue
        prospect = (sb.table("qualified_prospects").select("handle")
                    .eq("id", conv[0]["prospect_id"]).limit(1).execute()).data
        handle = prospect[0]["handle"] if prospect else "unknown"

        embed = _build_followup_embed(f, handle)
        try:
            msg = await channel.send(embed=embed)
        except Exception as e:
            log.error("m8.discord.followup_post_failed",
                      followup_id=f["id"], err=str(e))
            continue

        sb.table("followups").update(
            {"discord_message_id": str(msg.id)}
        ).eq("id", f["id"]).execute()


def _build_followup_embed(f: dict, handle: str) -> discord.Embed:
    embed = discord.Embed(
        title="👻 GHOST FOLLOWUP",
        description=f"**@{handle}** · followup **#{f.get('followup_number', 1)}**",
        color=discord.Color.dark_grey(),
    )
    embed.add_field(
        name="The followup",
        value=f"```\n{f.get('message_template', '')}\n```",
        inline=False,
    )
    embed.add_field(
        name="How to send",
        value=f"Open existing DM thread with @{handle} → long-press → paste.",
        inline=False,
    )
    embed.add_field(
        name="Mark complete",
        value=f"✅ `/followup_sent {f['id']}`",
        inline=False,
    )
    embed.set_footer(text=f"followup_id: {f['id']}")
    return embed


# ════════════════════════════════════════════════════════════════════
# OPERATOR SLASH COMMANDS
# ════════════════════════════════════════════════════════════════════

@bot.tree.command(name="follow_done", description="Mark a follow action as completed by you")
@app_commands.describe(action_id="The action UUID from the card footer")
async def follow_done(interaction: discord.Interaction, action_id: str):
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "human_completed",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "human_assignee": str(interaction.user.id),
    }).eq("id", action_id).execute()
    log.info("m8.discord.follow_done",
             action_id=action_id, user=str(interaction.user.id))
    await interaction.response.send_message(
        f"✅ follow done for {action_id}", ephemeral=True
    )


@bot.tree.command(name="like_done", description="Mark a like action as completed by you")
@app_commands.describe(action_id="The action UUID from the card footer")
async def like_done(interaction: discord.Interaction, action_id: str):
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "human_completed",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "human_assignee": str(interaction.user.id),
    }).eq("id", action_id).execute()
    await interaction.response.send_message(
        f"✅ like done for {action_id}", ephemeral=True
    )


@bot.tree.command(name="story_done", description="Mark a story view/like as completed")
@app_commands.describe(action_id="The action UUID from the card footer")
async def story_done(interaction: discord.Interaction, action_id: str):
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "human_completed",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "human_assignee": str(interaction.user.id),
    }).eq("id", action_id).execute()
    await interaction.response.send_message(
        f"✅ story done for {action_id}", ephemeral=True
    )


@bot.tree.command(name="story_skip", description="Skip a story action (no live story available)")
@app_commands.describe(action_id="The action UUID from the card footer")
async def story_skip(interaction: discord.Interaction, action_id: str):
    sb = get_supabase()
    sb.table("warming_actions").update({
        "status": "cancelled",
        "failure_reason": "no_active_stories",
        "human_assignee": str(interaction.user.id),
    }).eq("id", action_id).execute()
    await interaction.response.send_message(
        f"⏭️ story skipped for {action_id} (no active stories)", ephemeral=True
    )


@bot.tree.command(name="dm_sent", description="Mark an opener DM as sent")
@app_commands.describe(opener_id="The opener UUID from the card footer")
async def dm_sent(interaction: discord.Interaction, opener_id: str):
    sb = get_supabase()
    # Mark opener sent
    sb.table("openers").update({
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", opener_id).execute()

    # Look up prospect_id + create conversation so M7 starts polling for replies
    opener = (sb.table("openers").select("*")
              .eq("id", opener_id).limit(1).execute()).data
    if opener:
        op = opener[0]
        # Build the conversation row if it doesn't already exist
        existing = (sb.table("conversations").select("id")
                    .eq("prospect_id", op["prospect_id"]).limit(1).execute()).data
        if not existing:
            # Pick the burner that owns the conversation. Openers table doesn't
            # carry ig_account directly; for V1 fall back to the single default
            # burner. When multi-burner support lands, replace with a lookup
            # against the warming_actions row for this prospect.
            from app.core.supabase_client import get_supabase as _gs
            sb2 = _gs()
            ig_account_handle = op.get("ig_account") or "ignorethisdump2"
            sb2.table("conversations").insert({
                "prospect_id": op["prospect_id"],
                "ig_account": ig_account_handle,
                # 'opener' is the Selling Map stage value enforced by the
                # conversations_current_stage_check CHECK constraint
                # (opener / escalation / invitation / action / closed_won /
                # closed_lost / ghosted / handed_off). The Discord embed
                # uses 'opener_sent' for human readability but that string
                # is NOT a valid DB stage.
                "current_stage": "opener",
                "last_outbound_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

        # Update prospect status — 'conversation_active' is the schema-valid
        # status enforced by qualified_prospects_status_check. Allowed values:
        # pending_enrichment, enriched, pending_warmup, warming, warmed,
        # pending_opener, opener_ready, conversation_active, converted, dead,
        # handed_off_human.
        sb.table("qualified_prospects").update({"status": "conversation_active"}) \
            .eq("id", op["prospect_id"]).execute()

    log.info("m8.discord.dm_sent_confirmed",
             opener_id=opener_id, user=str(interaction.user.id))
    await interaction.response.send_message(
        f"✅ DM marked sent for opener {opener_id}", ephemeral=True
    )


@bot.tree.command(name="reply_sent", description="Mark an AI-drafted reply as sent (as-drafted)")
@app_commands.describe(pending_id="The pending message UUID from the card footer")
async def reply_sent(interaction: discord.Interaction, pending_id: str):
    sb = get_supabase()
    pending = (sb.table("pending_outbound_messages").select("*")
               .eq("id", pending_id).limit(1).execute()).data
    if not pending:
        await interaction.response.send_message(
            f"⚠️ pending message {pending_id} not found", ephemeral=True
        )
        return
    p = pending[0]

    # Persist the outbound message + advance stage now that the operator
    # has actually sent it in IG.
    from app.modules.m7_conversation import repository as m7_repo
    from app.modules.m7_conversation.selling_map import next_stage, IllegalTransition
    from app.modules.m7_conversation.decision import AgentDecision

    m7_repo.insert_outbound_message(
        conversation_id=p["conversation_id"],
        body=p["message_text"],
        stage_at_time=p["stage_at_decision"],
        agent_decision=p["agent_decision_json"],
        ai_confidence=float(p["ai_confidence"]),
        triggered_handoff=False,
    )
    m7_repo.touch_conversation_outbound(p["conversation_id"])

    # Advance stage based on the original decision
    try:
        decision = AgentDecision(**p["agent_decision_json"])
        new_stage = next_stage(p["stage_at_decision"], decision)
        m7_repo.update_conversation_stage(p["conversation_id"], new_stage)
    except (IllegalTransition, Exception) as e:
        log.warning("m8.discord.reply_sent_stage_skip",
                    pending_id=pending_id, err=str(e))

    sb.table("pending_outbound_messages").update({
        "status": "operator_sent",
        "operator_assignee": str(interaction.user.id),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", pending_id).execute()

    log.info("m8.discord.reply_sent_confirmed",
             pending_id=pending_id, user=str(interaction.user.id))
    await interaction.response.send_message(
        f"✅ reply sent for {pending_id}", ephemeral=True
    )


@bot.tree.command(name="reply_edit", description="Mark a reply as sent with edited text")
@app_commands.describe(
    pending_id="The pending message UUID",
    text="The text you actually sent",
)
async def reply_edit(interaction: discord.Interaction, pending_id: str, text: str):
    sb = get_supabase()
    pending = (sb.table("pending_outbound_messages").select("*")
               .eq("id", pending_id).limit(1).execute()).data
    if not pending:
        await interaction.response.send_message(
            f"⚠️ pending {pending_id} not found", ephemeral=True
        )
        return
    p = pending[0]

    from app.modules.m7_conversation import repository as m7_repo
    m7_repo.insert_outbound_message(
        conversation_id=p["conversation_id"],
        body=text,
        stage_at_time=p["stage_at_decision"],
        agent_decision={**p["agent_decision_json"], "operator_edited": True},
        ai_confidence=float(p["ai_confidence"]),
        triggered_handoff=False,
    )
    m7_repo.touch_conversation_outbound(p["conversation_id"])

    sb.table("pending_outbound_messages").update({
        "status": "edited_then_sent",
        "operator_assignee": str(interaction.user.id),
        "operator_final_text": text,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", pending_id).execute()

    await interaction.response.send_message(
        f"✅ reply (edited) sent for {pending_id}", ephemeral=True
    )


@bot.tree.command(name="followup_sent", description="Mark a ghost followup as sent")
@app_commands.describe(followup_id="The followup UUID from the card footer")
async def followup_sent(interaction: discord.Interaction, followup_id: str):
    sb = get_supabase()
    f = (sb.table("followups").select("*")
         .eq("id", followup_id).limit(1).execute()).data
    if not f:
        await interaction.response.send_message(
            f"⚠️ followup {followup_id} not found", ephemeral=True
        )
        return
    row = f[0]

    from app.modules.m7_conversation import repository as m7_repo
    conv = m7_repo.get_conversation(row["conversation_id"])
    if conv:
        m7_repo.insert_outbound_message(
            conversation_id=row["conversation_id"],
            body=row["message_template"],
            stage_at_time=conv["current_stage"],
            agent_decision={"source": "ghost_followup",
                            "followup_number": row["followup_number"]},
            ai_confidence=1.0,
            triggered_handoff=False,
        )
        m7_repo.touch_conversation_outbound(row["conversation_id"])
        m7_repo.increment_ghost_count(row["conversation_id"])

    m7_repo.mark_followup_sent(followup_id)

    await interaction.response.send_message(
        f"✅ followup {followup_id} marked sent", ephemeral=True
    )


@bot.tree.command(name="queue_all", description="Show your full operator queue, grouped by type")
async def queue_all(interaction: discord.Interaction):
    sb = get_supabase()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Count each queue
    warmups = (sb.table("warming_actions").select("action", count="exact")
               .eq("status", "skipped_human_queue")
               .lte("scheduled_for", now_iso)
               .execute())
    warmup_rows = warmups.data or []
    by_action: dict[str, int] = {}
    for r in warmup_rows:
        by_action[r["action"]] = by_action.get(r["action"], 0) + 1

    pending_dms = (sb.table("openers").select("id", count="exact")
                   .eq("approved_for_send", True)
                   .is_("sent_at", "null").execute()).count or 0
    pending_replies = (sb.table("pending_outbound_messages")
                       .select("id", count="exact")
                       .eq("status", "awaiting_operator").execute()).count or 0
    pending_followups = (sb.table("followups").select("id", count="exact")
                         .eq("status", "ready_for_operator").execute()).count or 0

    lines = ["**Your operator queue right now:**", ""]
    if pending_dms:
        lines.append(f"📨  **{pending_dms}** DMs to send")
    if pending_replies:
        lines.append(f"💬  **{pending_replies}** reply drafts ready")
    if pending_followups:
        lines.append(f"👻  **{pending_followups}** ghost followups")
    if by_action.get("follow"):
        lines.append(f"🟢  **{by_action['follow']}** follows")
    if by_action.get("like_post"):
        lines.append(f"💛  **{by_action['like_post']}** likes")
    if by_action.get("story_view") or by_action.get("story_like"):
        s = (by_action.get("story_view", 0) + by_action.get("story_like", 0))
        lines.append(f"❤️  **{s}** story interactions")
    if by_action.get("comment"):
        lines.append(f"💬  **{by_action['comment']}** vibe-comments")

    if len(lines) == 2:
        lines.append("_Queue empty — go enjoy your day._")

    await interaction.response.send_message(
        "\n".join(lines), ephemeral=True
    )


# ════════════════════════════════════════════════════════════════════
# INPUT SURFACE — seed niche handles + hashtags via Discord
# ════════════════════════════════════════════════════════════════════
# These commands let a non-technical operator populate the discovery
# pipeline's input tables without touching SQL or Supabase Studio.
#
# Two input streams feed M1+M2 discovery:
#   1. crawl_queue (seed handles) — M1 fans out from these to their
#      tagged-photo network, finding business owners in their circle
#   2. hashtags — M2 scrapes top+recent posts per tag, pulling new
#      accounts in the niche into the system
#
# Both /seed_add and /hashtag_add accept comma-separated values so the
# operator can paste 20 entries at once instead of running the command
# 20 times.
# ════════════════════════════════════════════════════════════════════


def _normalize_handle(raw: str) -> str:
    """Wrapper for app.core.input_normalize.normalize_handle."""
    from app.core.input_normalize import normalize_handle
    return normalize_handle(raw)


def _normalize_hashtag(raw: str) -> str:
    """Wrapper for app.core.input_normalize.normalize_hashtag."""
    from app.core.input_normalize import normalize_hashtag
    return normalize_hashtag(raw)


@bot.tree.command(name="seed_add",
                  description="Add IG handle(s) to the discovery seed queue. Comma-separate for bulk.")
@app_commands.describe(
    handles="One handle or comma-separated handles. URLs OK. @ optional.",
    niche="Niche tag (e.g. health_coaching, ecom_coaching, course_creators)",
)
async def seed_add(interaction: discord.Interaction, handles: str, niche: str):
    sb = get_supabase()
    items = [_normalize_handle(h) for h in handles.split(",") if h.strip()]
    items = [h for h in items if h]  # filter empty after normalize

    if not items:
        await interaction.response.send_message(
            "⚠️ No valid handles found in input.", ephemeral=True
        )
        return

    inserted = 0
    skipped = 0
    errors = []
    for handle in items:
        try:
            sb.table("crawl_queue").insert({
                "handle": handle,
                "depth": 0,
                "parent_seed": f"manual:{niche}",
                "priority": 3,           # higher priority than default 5
                "status": "pending",
            }).execute()
            inserted += 1
        except Exception as e:
            err_str = str(e)
            # Unique constraint violations = already seeded → skip, not error
            if "duplicate" in err_str.lower() or "unique" in err_str.lower():
                skipped += 1
            else:
                errors.append(f"{handle}: {err_str[:60]}")

    log.info("m8.discord.seed_add",
             user=str(interaction.user.id), niche=niche,
             inserted=inserted, skipped=skipped, error_count=len(errors))

    msg_parts = [f"✅ **{inserted}** seed(s) added to niche `{niche}`"]
    if skipped:
        msg_parts.append(f"⏭️ {skipped} already in queue (skipped)")
    if errors:
        msg_parts.append(f"⚠️ {len(errors)} errors:\n" + "\n".join(errors[:5]))
    await interaction.response.send_message(
        "\n".join(msg_parts), ephemeral=True
    )


@bot.tree.command(name="seeds_list",
                  description="Show recently-added seed handles, optionally filtered by niche")
@app_commands.describe(
    niche="Optional: filter by niche tag",
    limit="How many to show (default 15, max 30)",
)
async def seeds_list(interaction: discord.Interaction,
                     niche: Optional[str] = None,
                     limit: Optional[int] = 15):
    limit = min(max(limit or 15, 1), 30)
    sb = get_supabase()
    q = (sb.table("crawl_queue").select("handle,parent_seed,status,enqueued_at")
         .order("enqueued_at", desc=True)
         .limit(limit))
    if niche:
        q = q.like("parent_seed", f"%{niche}%")
    rows = q.execute().data or []

    if not rows:
        msg = "_No seeds found._"
        if niche:
            msg = f"_No seeds found for niche `{niche}`._"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    lines = [f"**Last {len(rows)} seeds:**"]
    for r in rows:
        parent = (r.get("parent_seed") or "").replace("manual:", "")
        status_emoji = {
            "pending": "⏳", "claimed": "🔄", "done": "✅",
            "failed": "❌", "skipped": "⏭️",
        }.get(r.get("status"), "·")
        lines.append(
            f"{status_emoji} `@{r['handle']}` · niche: `{parent}`"
        )
    await interaction.response.send_message(
        "\n".join(lines), ephemeral=True
    )


@bot.tree.command(name="hashtag_add",
                  description="Add niche hashtag(s) for M2 to scrape. Comma-separate for bulk.")
@app_commands.describe(
    tags="One tag or comma-separated tags. # optional (e.g. learndropshipping or #learndropshipping)",
    niche="Niche tag (e.g. health_coaching, ecom_coaching, course_creators)",
)
async def hashtag_add(interaction: discord.Interaction, tags: str, niche: str):
    sb = get_supabase()
    items = [_normalize_hashtag(t) for t in tags.split(",") if t.strip()]
    items = [t for t in items if t]

    if not items:
        await interaction.response.send_message(
            "⚠️ No valid hashtags found in input.", ephemeral=True
        )
        return

    inserted = 0
    skipped = 0
    errors = []
    for tag in items:
        try:
            sb.table("hashtags").insert({
                "tag": tag,
                "niche": niche,
                "source": "manual",
                "active": True,
            }).execute()
            inserted += 1
        except Exception as e:
            err_str = str(e)
            if "duplicate" in err_str.lower() or "unique" in err_str.lower():
                skipped += 1
            else:
                errors.append(f"#{tag}: {err_str[:60]}")

    log.info("m8.discord.hashtag_add",
             user=str(interaction.user.id), niche=niche,
             inserted=inserted, skipped=skipped, error_count=len(errors))

    msg_parts = [f"✅ **{inserted}** hashtag(s) added to niche `{niche}`"]
    if skipped:
        msg_parts.append(f"⏭️ {skipped} already exist (skipped)")
    if errors:
        msg_parts.append(f"⚠️ {len(errors)} errors:\n" + "\n".join(errors[:5]))
    await interaction.response.send_message(
        "\n".join(msg_parts), ephemeral=True
    )


@bot.tree.command(name="hashtags_list",
                  description="Show seeded hashtags, optionally filtered by niche")
@app_commands.describe(
    niche="Optional: filter by niche",
    show_inactive="Include inactive (paused) hashtags",
)
async def hashtags_list(interaction: discord.Interaction,
                        niche: Optional[str] = None,
                        show_inactive: Optional[bool] = False):
    sb = get_supabase()
    q = (sb.table("hashtags")
         .select("tag,niche,active,last_scraped_at,yield_qualified_30d")
         .order("niche", desc=False)
         .order("tag", desc=False)
         .limit(50))
    if niche:
        q = q.eq("niche", niche)
    if not show_inactive:
        q = q.eq("active", True)
    rows = q.execute().data or []

    if not rows:
        msg = "_No hashtags found._"
        if niche:
            msg = f"_No hashtags found for niche `{niche}`._"
        await interaction.response.send_message(msg, ephemeral=True)
        return

    # Group by niche
    by_niche: dict[str, list[dict]] = {}
    for r in rows:
        by_niche.setdefault(r.get("niche") or "unknown", []).append(r)

    lines = [f"**{len(rows)} hashtag(s) seeded:**", ""]
    for niche_name, items in sorted(by_niche.items()):
        lines.append(f"**`{niche_name}`** ({len(items)})")
        for r in items[:10]:   # cap per-niche so the embed fits
            status = "" if r.get("active") else " ⏸️"
            yield_str = ""
            if r.get("yield_qualified_30d"):
                yield_str = f" · yield: {r['yield_qualified_30d']}"
            lines.append(f"  · `#{r['tag']}`{status}{yield_str}")
        if len(items) > 10:
            lines.append(f"  · _…and {len(items) - 10} more_")
        lines.append("")

    # Discord message limit ~2000 chars; truncate if needed
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[:1850] + "\n_…output truncated, use niche filter to narrow_"

    await interaction.response.send_message(content, ephemeral=True)




def run() -> None:
    if not TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN not set")
    if not CHANNEL_ID:
        raise RuntimeError("DISCORD_HANDOFF_CHANNEL_ID not set")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    run()
