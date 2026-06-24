-- ============================================================================
-- Migration 002 — Mason Corpus + Opener Archetype
-- Run AFTER 001_initial_schema.sql
-- ============================================================================

-- ----------------------------------------------------------------------------
-- mason_corpus — verbatim templates, examples, and phrasings from Mason's
-- teaching. M6 (opener) and M7 (conversation) read from this at runtime.
-- ----------------------------------------------------------------------------

CREATE TABLE mason_corpus (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  category TEXT NOT NULL CHECK (category IN (
    'opener_personal_hook',          -- "saw your Hawaii photos..." style
    'opener_gap_hook',               -- "noticed a flaw on your site..." style
    'opener_cross_platform',         -- "saw you on TikTok but hitting on IG" style
    'opener_curiosity_phrase',       -- "got a sec?", "mind if I share?", etc.
    'escalation_example',            -- mid-convo examples
    'invitation_example',            -- "mind if I send you a quick checklist?"
    'action_example',                -- "cool, here's the link"
    'micro_commitment',              -- "yes", "sure", "send it"
    'objection_uncertainty_reframe', -- verbatim reframe for "I'm not sure"
    'objection_overwhelm_reframe',   -- verbatim reframe for "too much right now"
    'followup_template',             -- "no rush, just leaving this here..."
    'anti_pattern'                   -- things NOT to do
  )),
  text TEXT NOT NULL,                -- the verbatim or paraphrased content
  is_verbatim BOOLEAN NOT NULL DEFAULT true,
  niche_filter TEXT,                 -- 'ecom' | 'coaching' | 'influencer' | NULL (all)
  notes TEXT,                        -- any context Mason gave (when to use, etc.)
  active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_mason_category ON mason_corpus(category, active);

-- ----------------------------------------------------------------------------
-- Extend openers table with archetype tracking
-- ----------------------------------------------------------------------------

ALTER TABLE openers ADD COLUMN archetype TEXT
  CHECK (archetype IN ('personal_hook','gap_hook','cross_platform_mismatch'));

ALTER TABLE openers ADD COLUMN mason_template_id UUID REFERENCES mason_corpus(id);
ALTER TABLE openers ADD COLUMN char_count INT;
ALTER TABLE openers ADD COLUMN fits_lockscreen_preview BOOLEAN;
ALTER TABLE openers ADD COLUMN uses_ellipsis BOOLEAN DEFAULT false;

-- ----------------------------------------------------------------------------
-- Extend gap_analysis with Mason's named gaps
-- ----------------------------------------------------------------------------

ALTER TABLE gap_analysis ADD COLUMN gap_local_seo BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN gap_homepage_conversion BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN gap_product_page_competitor BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN gap_email_revenue_underperform BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN gap_lead_magnet_missing BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN gap_content_struggle BOOLEAN;
ALTER TABLE gap_analysis ADD COLUMN cross_platform_discovery_source TEXT;
-- e.g. 'tiktok' or 'youtube' — fuels the cross_platform_mismatch opener

-- ----------------------------------------------------------------------------
-- Extend conversations with Mason's stage-specific tracking
-- ----------------------------------------------------------------------------

ALTER TABLE conversations ADD COLUMN replies_were_monosyllabic BOOLEAN DEFAULT false;
-- Mason: monosyllabic replies signal NOT YET in escalation
ALTER TABLE conversations ADD COLUMN signals_emotionally_in_motion BOOLEAN DEFAULT false;
-- Mason: "asking about process, structure, outcomes" = high-value

-- ----------------------------------------------------------------------------
-- Seed mason_corpus with verbatim content from NotebookLM extraction
-- ----------------------------------------------------------------------------

INSERT INTO mason_corpus (category, text, is_verbatim, niche_filter, notes) VALUES

-- Personal hook openers (verbatim from Mason)
('opener_personal_hook',
 'Hey, Robert, saw you uploaded some photos of your recent vacation to Hawaii. And I have a question. Got a sec?',
 true, NULL, 'Name + recent specific detail (vacation) + curiosity hook + permission ask'),

('opener_personal_hook',
 'Hey Gina, I saw your comment on the Savvy Entrepreneurs Group about struggling with content ideas. About a month ago, I found something that helped me generate a month''s worth of ideas in 45 minutes.',
 true, NULL, 'Name + specific external observation + open loop value proposition'),

('opener_personal_hook',
 'Hey Tom, always enjoy your insights on AI marketing. By the way, have you come across HeyGen? It''s a new AI app that makes photorealistic video avatars. Thought of you immediately when I saw it this morning. Because.',
 true, NULL, 'Compliment + value share + ends mid-sentence (open loop)'),

('opener_personal_hook',
 'Hey, I saw your post and it sparked a weird idea that I''ve never shared with anyone yet.',
 true, NULL, 'Curiosity through novelty + exclusivity'),

('opener_personal_hook',
 'Hey, saw your post on LinkedIn and I had a weird idea about it, but I''m 90% sure it will get me roasted. Want it?',
 true, NULL, 'Self-deprecating + curiosity + permission ask'),

-- Gap-hook openers (verbatim)
('opener_gap_hook',
 'I was just checking out your website and a small opportunity jumped out at me that many of your competitors are missing. It has to do with local SEO...',
 true, NULL, 'Local SEO gap + ellipsis'),

('opener_gap_hook',
 'I noticed on your website there''s a little flaw. Did you know that one small change to your homepage headline could potentially increase your conversions by up to 20%?',
 true, NULL, 'Homepage conversion gap + specific stat'),

('opener_gap_hook',
 'Your homepage is solid, but there''s one line that could be killing conversions.',
 true, NULL, 'Compliment + specific micro-gap (homepage line)'),

('opener_gap_hook',
 'I just compared your product page to five top sellers. You''re missing one thing that they all use.',
 true, 'ecom', 'Competitor comparison gap'),

('opener_gap_hook',
 'Are you producing the ballpark of 20 to 30% of Rev with just email right now?',
 true, 'ecom', 'Email revenue underperform gap'),

('opener_gap_hook',
 'I saw your post about burnout, and I have a strange idea for turning that into a lead magnet. Want it?',
 true, 'coaching', 'Content struggle / lead magnet gap'),

-- Cross-platform mismatch (Mason's signature)
('opener_cross_platform',
 'Saw you on TikTok, but hitting you on IG',
 true, NULL, 'Use as opener prefix when discovery_source != instagram'),

-- Curiosity phrases (verbatim sentence-enders)
('opener_curiosity_phrase', 'Got a sec?', true, NULL, NULL),
('opener_curiosity_phrase', 'Mind if I share a quick thought?', true, NULL, NULL),
('opener_curiosity_phrase', 'Would it be totally crazy if I shared...', true, NULL, 'Ellipsis intentional'),
('opener_curiosity_phrase', 'Not sure if this is relevant, but...', true, NULL, 'Ellipsis intentional'),
('opener_curiosity_phrase', 'Want it?', true, NULL, NULL),

-- Escalation stage example
('escalation_example',
 'I totally get that. I worked with a coach who felt the exact same way. Until we streamlined her offer and leads starting finding her. That changed everything. Her sales doubled overnight.',
 true, NULL, 'Empathy + relevant story + outcome'),

-- Invitation stage example
('invitation_example',
 'Hey, mind if I send you a quick checklist we made for that?',
 true, NULL, 'Soft permission-based offer'),

-- Action stage example
('action_example',
 'Cool, here''s the link.',
 true, NULL, 'Frictionless delivery'),

-- Micro-commitments (the Ladder)
('micro_commitment', 'Yes', true, NULL, 'Tier 1'),
('micro_commitment', 'Sure', true, NULL, 'Tier 1'),
('micro_commitment', 'Go ahead', true, NULL, 'Tier 1'),
('micro_commitment', 'Send it', true, NULL, 'Tier 1'),

-- Objection reframes
('objection_uncertainty_reframe',
 'Hey, that''s totally fair. Most people feel that way before trying something new... Rather than trying to prove it''s going to work, want to explore this together to see if it''s even a fit?',
 true, NULL, 'For "I''m not sure this is going to work for me"'),

('objection_overwhelm_reframe',
 'Honestly, that makes total sense. There''s a lot on your plate right now... What if instead we just take a peek at one thing that might help you today?',
 true, NULL, 'For "This feels like too much right now"'),

-- "No" response (validation, not pressure)
('objection_uncertainty_reframe',
 'Hey, that''s totally fair. Can I ask what makes you feel this way?',
 true, NULL, 'Generic "no" validation — open exploration not pressure'),

-- Follow-ups for ghosted leads (verbatim, 5 templates)
('followup_template',
 'No rush. Just wanted to leave this here in case it''s useful.',
 true, NULL, 'Followup #1, ~2 days after ghost'),

('followup_template',
 'Still happy to send you the link if it helps. No pressure at all.',
 true, NULL, 'Followup #2'),

('followup_template',
 'Hey, quick heads up. Spots are filling up for this week if that''s something you''re still considering.',
 true, NULL, 'Followup with light urgency — use sparingly'),

('followup_template',
 'Checking to make sure you saw this.',
 true, NULL, 'Short nudge'),

('followup_template',
 'Coming back to this because I know we''d crush together.',
 true, NULL, 'Re-engagement, weeks later'),

-- Anti-patterns (things to NEVER do)
('anti_pattern', 'Dear Robert, I''m writing to inform you', false, NULL, 'Corporate/formal tone — banned'),
('anti_pattern', 'I love you', false, NULL, 'Kiss-ass overcompliment — banned'),
('anti_pattern', 'This picture is so sick', false, NULL, 'Kiss-ass overcompliment — banned'),
('anti_pattern', 'Confusion doesn''t trigger curiosity. It triggers shutdown.', true, NULL, 'Vague/confusing hooks fail'),
('anti_pattern', 'Calendar link in first 2 messages', false, NULL, 'The "big ask" too soon = trap'),
('anti_pattern', 'More "I" and "me" than "you" and "your"', false, NULL, 'Self-focus banned');

-- ----------------------------------------------------------------------------
-- Enable RLS on new table
-- ----------------------------------------------------------------------------

ALTER TABLE mason_corpus ENABLE ROW LEVEL SECURITY;
