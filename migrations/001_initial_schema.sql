-- ============================================================================
-- FC IG Lead Discovery & Conversation Engine
-- Migration 001 — Initial Schema
-- ============================================================================
-- Run this in Supabase SQL Editor as the postgres role.
-- All tables use UUID primary keys, gen_random_uuid() defaults, and have RLS
-- enabled with service-role-only access (configured separately in Supabase UI).
-- ============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ----------------------------------------------------------------------------
-- M0 — Operator IG Accounts & Proxies
-- ----------------------------------------------------------------------------

CREATE TABLE ig_accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  handle TEXT UNIQUE NOT NULL,
  proxy_endpoint TEXT NOT NULL,             -- e.g. "http://user:pass@host:port"
  session_cookies_encrypted TEXT,           -- Fernet-encrypted JSON blob
  has_blue_check BOOLEAN DEFAULT false,
  daily_caps JSONB DEFAULT '{
    "follows": 30,
    "likes": 50,
    "comments": 5,
    "story_actions": 20,
    "profile_loads": 150,
    "hashtag_pages": 80,
    "dms_sent": 25
  }'::jsonb,
  current_status TEXT DEFAULT 'active'
    CHECK (current_status IN ('active','cooldown','banned','warming','disabled')),
  last_soft_block_at TIMESTAMPTZ,
  cooldown_until TIMESTAMPTZ,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE ig_account_usage (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ig_account TEXT NOT NULL REFERENCES ig_accounts(handle) ON DELETE CASCADE,
  usage_date DATE NOT NULL,
  follows INT DEFAULT 0,
  likes INT DEFAULT 0,
  comments INT DEFAULT 0,
  story_actions INT DEFAULT 0,
  profile_loads INT DEFAULT 0,
  hashtag_pages INT DEFAULT 0,
  dms_sent INT DEFAULT 0,
  UNIQUE(ig_account, usage_date)
);

CREATE INDEX idx_ig_usage_date ON ig_account_usage(usage_date DESC);

-- ----------------------------------------------------------------------------
-- M1/M2 — Discovery: Seeds, Hashtags, Accounts, Edges
-- ----------------------------------------------------------------------------

CREATE TABLE seeds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  handle TEXT UNIQUE NOT NULL,
  niche TEXT,
  added_by TEXT DEFAULT 'jon',
  notes TEXT,
  active BOOLEAN DEFAULT true,
  last_crawled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE hashtags (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tag TEXT UNIQUE NOT NULL,                 -- without '#'
  niche TEXT,
  source TEXT DEFAULT 'manual'
    CHECK (source IN ('manual','discovered')),
  active BOOLEAN DEFAULT true,
  last_scraped_at TIMESTAMPTZ,
  yield_qualified_30d INT DEFAULT 0,        -- updated by daily rollup
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE accounts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  handle TEXT UNIQUE NOT NULL,
  full_name TEXT,
  bio TEXT,
  follower_count INT,
  following_count INT,
  post_count INT,
  is_business BOOLEAN,
  external_url TEXT,
  profile_pic_url TEXT,
  discovered_via TEXT
    CHECK (discovered_via IN ('tagged','hashtag','manual','cross_platform')),
  discovered_from TEXT,                     -- parent seed/hashtag/handle
  depth INT DEFAULT 0,                      -- hops from seed
  raw_json JSONB,
  first_seen_at TIMESTAMPTZ DEFAULT NOW(),
  last_refreshed_at TIMESTAMPTZ
);

CREATE INDEX idx_accounts_handle_trgm ON accounts USING gin (handle gin_trgm_ops);
CREATE INDEX idx_accounts_discovered_via ON accounts(discovered_via);
CREATE INDEX idx_accounts_first_seen ON accounts(first_seen_at DESC);

CREATE TABLE tag_edges (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  from_handle TEXT NOT NULL,
  to_handle TEXT NOT NULL,
  via_post_url TEXT,
  discovered_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(from_handle, to_handle, via_post_url)
);

CREATE INDEX idx_tag_edges_from ON tag_edges(from_handle);
CREATE INDEX idx_tag_edges_to ON tag_edges(to_handle);

-- Persistent crawl queue (survives container restarts)
CREATE TABLE crawl_queue (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  handle TEXT NOT NULL,
  depth INT NOT NULL DEFAULT 0,
  parent_seed TEXT,
  priority INT DEFAULT 5,                   -- 1=highest, 10=lowest
  claimed_by TEXT,                          -- worker id, NULL if unclaimed
  claimed_at TIMESTAMPTZ,
  status TEXT DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','done','failed','skipped')),
  attempts INT DEFAULT 0,
  last_error TEXT,
  enqueued_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(handle)                            -- dedupe at enqueue time
);

CREATE INDEX idx_crawl_queue_status_priority ON crawl_queue(status, priority, enqueued_at)
  WHERE status = 'pending';

-- ----------------------------------------------------------------------------
-- M3 — Cross-Platform Research & Gap Identification
-- ----------------------------------------------------------------------------

CREATE TABLE cross_platform_profiles (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE,
  handle TEXT NOT NULL,
  platform TEXT NOT NULL
    CHECK (platform IN ('tiktok','youtube','twitter','website')),
  platform_handle TEXT,                     -- their username on that platform
  platform_url TEXT,
  follower_count INT,
  has_active_content BOOLEAN,               -- posted in last 30d
  last_post_at TIMESTAMPTZ,
  scraped_at TIMESTAMPTZ DEFAULT NOW(),
  raw_data JSONB
);

CREATE INDEX idx_cpp_account ON cross_platform_profiles(account_id);

CREATE TABLE gap_analysis (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE UNIQUE,
  has_website BOOLEAN,
  has_email_capture BOOLEAN,
  has_lead_magnet BOOLEAN,
  has_paid_offer BOOLEAN,
  has_youtube BOOLEAN,
  has_tiktok BOOLEAN,
  primary_gap TEXT,                         -- the most exploitable gap for opener
  gap_evidence TEXT,                        -- specific URL/observation backing the gap
  analyzed_at TIMESTAMPTZ DEFAULT NOW()
);

-- ----------------------------------------------------------------------------
-- M4 — Qualification
-- ----------------------------------------------------------------------------

CREATE TABLE qualified_prospects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id UUID REFERENCES accounts(id) ON DELETE CASCADE UNIQUE,
  handle TEXT NOT NULL,
  pre_filter_score INT,
  link_crawl_score INT,
  cross_platform_score INT,
  total_score INT,
  link_in_bio TEXT,
  link_resolved_to TEXT,                    -- 'apply'|'calendly'|'kajabi'|...
  link_crawl_html_snapshot TEXT,
  is_high_value BOOLEAN DEFAULT false,      -- M8 handoff trigger
  high_value_reason TEXT,
  qualified_at TIMESTAMPTZ DEFAULT NOW(),
  status TEXT DEFAULT 'pending_enrichment'
    CHECK (status IN (
      'pending_enrichment','enriched','pending_warmup',
      'warming','warmed','pending_opener','opener_ready',
      'conversation_active','converted','dead','handed_off_human'
    ))
);

CREATE INDEX idx_qp_status ON qualified_prospects(status);
CREATE INDEX idx_qp_score ON qualified_prospects(total_score DESC);

-- ----------------------------------------------------------------------------
-- M5 — Warm-Up Actions
-- ----------------------------------------------------------------------------

CREATE TABLE warming_actions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id UUID REFERENCES qualified_prospects(id) ON DELETE CASCADE,
  ig_account TEXT NOT NULL REFERENCES ig_accounts(handle),
  action TEXT NOT NULL
    CHECK (action IN ('follow','like_post','comment','story_view','story_like','story_reply')),
  target_url TEXT,
  scheduled_for TIMESTAMPTZ NOT NULL,
  executed_at TIMESTAMPTZ,
  status TEXT DEFAULT 'scheduled'
    CHECK (status IN ('scheduled','executed','failed','skipped_human_queue','human_completed','cancelled')),
  human_payload JSONB,                      -- for comment: prospect context for human
  human_assignee TEXT,                      -- discord user id who claimed it
  failure_reason TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_warming_scheduled ON warming_actions(scheduled_for)
  WHERE status = 'scheduled';
CREATE INDEX idx_warming_prospect ON warming_actions(prospect_id);

-- ----------------------------------------------------------------------------
-- M6 — Opener (S.I.P.E.) Generation
-- ----------------------------------------------------------------------------

CREATE TABLE openers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id UUID REFERENCES qualified_prospects(id) ON DELETE CASCADE,
  opener_text TEXT NOT NULL,
  sipe_short BOOLEAN,                       -- under length target
  sipe_incomplete BOOLEAN,                  -- has the curiosity hook
  sipe_personal BOOLEAN,                    -- references specific prospect detail
  sipe_emotional BOOLEAN,                   -- has emotional word/phrase
  voice_match_score INT,                    -- 0-100, "smart curious friend"
  hooked_on_gap TEXT,                       -- which gap from gap_analysis
  claude_model TEXT,
  claude_raw_response JSONB,
  cost_usd NUMERIC(10,6),
  approved_for_send BOOLEAN DEFAULT false,
  approved_by TEXT,
  approved_at TIMESTAMPTZ,
  generated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_openers_prospect ON openers(prospect_id);

-- ----------------------------------------------------------------------------
-- M7 — Conversational Engine (Selling Map state)
-- ----------------------------------------------------------------------------

CREATE TABLE conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prospect_id UUID REFERENCES qualified_prospects(id) ON DELETE CASCADE UNIQUE,
  ig_account TEXT NOT NULL REFERENCES ig_accounts(handle),
  current_stage TEXT NOT NULL DEFAULT 'opener'
    CHECK (current_stage IN ('opener','escalation','invitation','action','closed_won','closed_lost','ghosted','handed_off')),
  stage_entered_at TIMESTAMPTZ DEFAULT NOW(),
  micro_commitments_obtained JSONB DEFAULT '[]'::jsonb,  -- ["link?", "10min chat?", ...]
  objections_handled JSONB DEFAULT '[]'::jsonb,
  last_inbound_at TIMESTAMPTZ,
  last_outbound_at TIMESTAMPTZ,
  ghost_followup_count INT DEFAULT 0,
  ai_confidence_avg NUMERIC(5,2),           -- rolling avg, used for handoff
  human_intervention BOOLEAN DEFAULT false,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_conv_stage ON conversations(current_stage);
CREATE INDEX idx_conv_prospect ON conversations(prospect_id);

CREATE TABLE messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  direction TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
  body TEXT NOT NULL,
  sent_at TIMESTAMPTZ,
  received_at TIMESTAMPTZ,
  stage_at_time TEXT,                       -- which stage the convo was in
  agent_decision JSONB,                     -- Claude's full reasoning for outbound
  ai_confidence NUMERIC(5,2),               -- 0-100
  triggered_handoff BOOLEAN DEFAULT false,
  ig_message_id TEXT,                       -- IG's internal id if captured
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_messages_conv ON messages(conversation_id, created_at);

-- Scheduled follow-ups for ghosted leads
CREATE TABLE followups (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  followup_number INT NOT NULL,             -- 1st, 2nd, 3rd nudge
  scheduled_for TIMESTAMPTZ NOT NULL,
  message_template TEXT,
  status TEXT DEFAULT 'scheduled'
    CHECK (status IN ('scheduled','sent','cancelled_response','cancelled_dead')),
  sent_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_followups_scheduled ON followups(scheduled_for)
  WHERE status = 'scheduled';

-- ----------------------------------------------------------------------------
-- M8 — Human-in-Loop Handoffs
-- ----------------------------------------------------------------------------

CREATE TABLE handoffs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
  prospect_id UUID REFERENCES qualified_prospects(id),
  trigger_reason TEXT NOT NULL
    CHECK (trigger_reason IN ('high_value','low_confidence','nuance_required','user_requested','objection_escalation')),
  trigger_detail TEXT,
  conversation_snapshot JSONB,              -- full message history at time of handoff
  ai_recommended_action TEXT,
  discord_message_id TEXT,
  assigned_to TEXT,
  status TEXT DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','resolved','returned_to_ai')),
  resolved_at TIMESTAMPTZ,
  resolution_notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_handoffs_status ON handoffs(status, created_at);

-- ----------------------------------------------------------------------------
-- M9 — Metrics & Monitoring
-- ----------------------------------------------------------------------------

CREATE TABLE daily_metrics (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  metric_date DATE NOT NULL UNIQUE,
  candidates_discovered INT DEFAULT 0,
  qualified INT DEFAULT 0,
  enriched INT DEFAULT 0,
  warmups_completed INT DEFAULT 0,
  openers_sent INT DEFAULT 0,
  replies_received INT DEFAULT 0,
  conversations_advanced INT DEFAULT 0,
  invitations_sent INT DEFAULT 0,
  closes INT DEFAULT 0,
  handoffs_triggered INT DEFAULT 0,
  soft_blocks_today INT DEFAULT 0,
  claude_cost_usd NUMERIC(10,4) DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Error log (system-wide)
CREATE TABLE error_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  module TEXT NOT NULL,                     -- 'm1', 'm7_agent', etc.
  severity TEXT NOT NULL CHECK (severity IN ('debug','info','warn','error','critical')),
  message TEXT NOT NULL,
  context JSONB,
  stack_trace TEXT,
  occurred_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_errors_time ON error_log(occurred_at DESC);
CREATE INDEX idx_errors_module ON error_log(module, occurred_at DESC);

-- ----------------------------------------------------------------------------
-- Triggers — auto-update updated_at
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_conv_updated
  BEFORE UPDATE ON conversations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ----------------------------------------------------------------------------
-- RLS — enable on all tables, default deny (service-role bypasses)
-- ----------------------------------------------------------------------------

DO $$
DECLARE
  t TEXT;
BEGIN
  FOR t IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public'
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- Done.
-- ----------------------------------------------------------------------------
