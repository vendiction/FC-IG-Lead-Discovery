-- ============================================================
-- Seed hashtags for M2 hashtag scraper to actually have work.
--
-- M2 worker is already running inside `worker_disc` and polling
-- the `hashtags` table every 10 min. It has nothing to do because
-- the table is empty. This populates it with a starter set across
-- Mason-relevant niches.
--
-- Run once. ON CONFLICT (tag) DO NOTHING makes this idempotent.
-- ============================================================

INSERT INTO hashtags (tag, niche, source, active) VALUES
  -- "Mason's Way" original example — info-marketers + dropship coaches
  ('learndropshipping',       'ecom_coaching',        'manual', true),
  ('dropshipcoach',           'ecom_coaching',        'manual', true),
  ('ecommercemastery',        'ecom_coaching',        'manual', true),
  ('shopifyhelp',             'ecom_coaching',        'manual', true),

  -- Health coaches — matches the qualified-prospects pattern already
  -- producing high-value leads (healthcoachinst, healthcoachclaudia)
  ('healthcoachlife',         'health_coaching',      'manual', true),
  ('certifiedhealthcoach',    'health_coaching',      'manual', true),
  ('functionalmedicinecoach', 'health_coaching',      'manual', true),
  ('perimenopausecoach',      'health_coaching',      'manual', true),
  ('hormonehealthcoach',      'health_coaching',      'manual', true),

  -- Business/copywriting coaches — adjacent to FC's own ICP
  ('copywritingcoach',        'business_coaching',    'manual', true),
  ('businessmentor',          'business_coaching',    'manual', true),
  ('onlinecoachingbusiness',  'business_coaching',    'manual', true),
  ('coachingbusiness',        'business_coaching',    'manual', true),

  -- Course creators — high-likelihood lead-magnet/email-list gaps
  ('coursecreator',           'course_creators',      'manual', true),
  ('digitalcoursecreator',    'course_creators',      'manual', true),
  ('selldigitalproducts',     'course_creators',      'manual', true)
ON CONFLICT (tag) DO NOTHING;

-- Verify
SELECT tag, niche, active, last_scraped_at FROM hashtags ORDER BY niche, tag;
