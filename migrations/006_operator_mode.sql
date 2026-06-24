-- ============================================================
-- Operator Mode — route every IG write action through Discord
-- ============================================================
-- Before this change: M5 follow/like/story/comment, M6 opener send,
-- M7 reply, and M8 ghost followups all ran through Playwright write
-- actions, which IG rate-limits and shadow-restricts aggressively.
--
-- After this change: all of the above queue cards to a human operator
-- in Discord, who taps Follow/Like/Heart/etc. on their own phone and
-- pastes DM bodies into the IG app. Same state machine, different
-- final delivery medium.
--
-- The Playwright write paths remain in the codebase as fallback /
-- V2 path with a healthy burner, but are not exercised by default.
-- ============================================================

-- 1. Track Discord message ID on openers so the bot doesn't re-queue.
ALTER TABLE openers
  ADD COLUMN IF NOT EXISTS discord_message_id TEXT;

-- 2. Same on followups (ghost follow-up ladder).
ALTER TABLE followups
  ADD COLUMN IF NOT EXISTS discord_message_id TEXT;

-- 3. New table for M7 outbound replies that are drafted by the AI but
--    awaiting human paste-and-send. Lets us defer the actual outbound
--    persistence + stage advancement until the operator confirms.
CREATE TABLE IF NOT EXISTS pending_outbound_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  prospect_handle TEXT NOT NULL,
  ig_account TEXT NOT NULL REFERENCES ig_accounts(handle),

  -- The AI-drafted message body the operator is asked to send
  message_text TEXT NOT NULL,

  -- Stage transition + agent decision snapshot — applied when operator confirms
  stage_at_decision TEXT NOT NULL,
  agent_decision_json JSONB NOT NULL,
  ai_confidence NUMERIC NOT NULL,

  -- Snapshot of the conversation history at the time we asked, for the embed
  history_snapshot JSONB,

  -- Operator workflow
  status TEXT DEFAULT 'awaiting_operator'
    CHECK (status IN ('awaiting_operator', 'operator_sent', 'cancelled', 'edited_then_sent')),
  discord_message_id TEXT,
  operator_assignee TEXT,                 -- discord user id
  operator_final_text TEXT,               -- what they actually sent if they edited
  resolved_at TIMESTAMPTZ,

  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS pending_outbound_status_idx
  ON pending_outbound_messages (status, created_at);

-- 4. Add 'operator_queued' to the message direction-tracking schema if needed.
--    The messages table itself doesn't need a new status — we just don't insert
--    into it until the operator confirms.
