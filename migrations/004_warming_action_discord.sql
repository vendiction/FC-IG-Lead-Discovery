-- ============================================================================
-- Migration 004 — Warming-action Discord surface
-- ============================================================================
-- The Discord bot's poll_warming_comments loop needs to know which comment
-- tasks it has already posted, mirroring how openers tracks
-- discord_review_message_id from migration 002.
--
-- Also adds a partial index for the bot's main query: comments queued for
-- human review that haven't been posted yet.
--
-- Safe to re-run — uses IF NOT EXISTS guards.
-- ============================================================================

ALTER TABLE warming_actions
  ADD COLUMN IF NOT EXISTS discord_message_id TEXT;

-- Optional free-form text the human types when marking the comment done.
-- Lets us audit what was actually posted vs. what the bot suggested.
ALTER TABLE warming_actions
  ADD COLUMN IF NOT EXISTS human_response_text TEXT;

ALTER TABLE warming_actions
  ADD COLUMN IF NOT EXISTS human_completed_at TIMESTAMPTZ;

-- Partial index — the Discord bot's only hot query is
-- "comment tasks queued for human, not yet posted to discord".
CREATE INDEX IF NOT EXISTS idx_warming_comments_unposted
  ON warming_actions(scheduled_for)
  WHERE action = 'comment'
    AND status = 'skipped_human_queue'
    AND discord_message_id IS NULL;
