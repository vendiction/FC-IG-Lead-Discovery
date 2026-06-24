-- ============================================================================
-- Migration 003 — Celebrity disqualifier columns on qualified_prospects
-- ============================================================================
-- The M4 worker has been writing these fields since the celeb DQ feature
-- shipped, but the original 001 migration didn't add them — Supabase has
-- been silently dropping the unknown keys, OR a manual ALTER was applied
-- in production. This makes the schema reproducible from scratch.
--
-- Safe to re-run — uses IF NOT EXISTS guards.
-- ============================================================================

ALTER TABLE qualified_prospects
  ADD COLUMN IF NOT EXISTS is_celebrity_disqualified BOOLEAN DEFAULT false;

ALTER TABLE qualified_prospects
  ADD COLUMN IF NOT EXISTS celebrity_dq_reason TEXT;

-- Partial index for the (rare) "list all DQ'd celebs" debugging query.
-- Cheap because >99% of rows have the default false.
CREATE INDEX IF NOT EXISTS idx_qp_celebrity_dq
  ON qualified_prospects(id)
  WHERE is_celebrity_disqualified = true;
