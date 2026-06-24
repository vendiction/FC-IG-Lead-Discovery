-- ============================================================================
-- Migration 002 — Opener review surface
-- ============================================================================
-- Adds a column on the openers table so the Discord bot (when configured) can
-- track which openers have already been posted to the review channel, and a
-- partial index that makes the "approved but not yet sent" query cheap.
--
-- Safe to re-run — uses IF NOT EXISTS guards.
-- ============================================================================

ALTER TABLE openers
  ADD COLUMN IF NOT EXISTS discord_review_message_id TEXT;

-- Track when the opener was actually sent (distinct from approved_at).
-- approved_at = "human said go"; sent_at = "DM landed in IG".
ALTER TABLE openers
  ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;

ALTER TABLE openers
  ADD COLUMN IF NOT EXISTS send_failure_reason TEXT;

-- Partial index — only the small subset of approved-but-not-yet-sent rows.
-- This is the sender's main query, so it deserves an index.
CREATE INDEX IF NOT EXISTS idx_openers_approved_unsent
  ON openers(approved_at)
  WHERE approved_for_send = true AND sent_at IS NULL;
