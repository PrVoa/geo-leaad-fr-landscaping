-- ============================================================
-- VAO Outreach Bot — Migration Schema
-- À exécuter dans Supabase Dashboard > SQL Editor
-- Réutilise la table landscapers existante (21K prospects)
-- ============================================================

-- ────────────────────────────────────────────────────────────
-- 1. Colonnes outreach sur landscapers (enrichissement + campagne)
-- ────────────────────────────────────────────────────────────

ALTER TABLE landscapers
  -- Enrichissement site web
  ADD COLUMN IF NOT EXISTS has_contact_form    BOOLEAN,
  ADD COLUMN IF NOT EXISTS contact_form_url    TEXT,
  ADD COLUMN IF NOT EXISTS form_html_snapshot  TEXT,
  ADD COLUMN IF NOT EXISTS form_fields_mapping JSONB,
  ADD COLUMN IF NOT EXISTS site_keywords       TEXT[],
  ADD COLUMN IF NOT EXISTS activity_types      TEXT[],
  ADD COLUMN IF NOT EXISTS site_quality_score  INT,
  ADD COLUMN IF NOT EXISTS has_portfolio       BOOLEAN,
  ADD COLUMN IF NOT EXISTS region              TEXT,

  -- Scoring outreach (séparé du score_icp existant pour ne pas casser le CRM)
  ADD COLUMN IF NOT EXISTS outreach_score      FLOAT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS tier                INT,
  ADD COLUMN IF NOT EXISTS scoring_details     JSONB,

  -- Campagne outreach
  ADD COLUMN IF NOT EXISTS campaign_status     TEXT DEFAULT 'new',
  ADD COLUMN IF NOT EXISTS current_sequence_step INT DEFAULT 0,
  ADD COLUMN IF NOT EXISTS next_action_date    DATE,
  ADD COLUMN IF NOT EXISTS next_action_type    TEXT,
  ADD COLUMN IF NOT EXISTS enriched_at         TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS scored_at           TIMESTAMPTZ;

-- Contrainte sur campaign_status
ALTER TABLE landscapers DROP CONSTRAINT IF EXISTS landscapers_campaign_status_check;
ALTER TABLE landscapers ADD CONSTRAINT landscapers_campaign_status_check CHECK (
  campaign_status IS NULL OR campaign_status IN (
    'new',
    'enrichment_pending',
    'enrichment_failed',
    'enriched',
    'scored',
    'in_sequence',
    'sequence_complete',
    'responded',
    'call_scheduled',
    'called',
    'demo_booked',
    'converted',
    'not_interested',
    'opted_out',
    'no_form',
    'invalid'
  )
);

-- Index outreach
CREATE INDEX IF NOT EXISTS idx_landscapers_campaign_status
  ON landscapers(campaign_status);
CREATE INDEX IF NOT EXISTS idx_landscapers_next_action
  ON landscapers(next_action_date, next_action_type)
  WHERE campaign_status IN ('scored', 'in_sequence', 'call_scheduled');
CREATE INDEX IF NOT EXISTS idx_landscapers_tier
  ON landscapers(tier) WHERE tier IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_landscapers_outreach_score
  ON landscapers(outreach_score DESC);

-- Mapping département → région (pour personnalisation des messages)
UPDATE landscapers SET region = CASE
  WHEN dept IN ('75','92','93','94','78','91','95','77') THEN 'Île-de-France'
  WHEN dept IN ('13','83','84','04','05','06') THEN 'Provence-Alpes-Côte d''Azur'
  WHEN dept IN ('31','32','09','12','46','65','81','82') THEN 'Occitanie'
  WHEN dept IN ('34','11','30','48','66') THEN 'Occitanie'
  WHEN dept IN ('33','24','40','47','64') THEN 'Nouvelle-Aquitaine'
  WHEN dept IN ('16','17','19','23','79','86','87') THEN 'Nouvelle-Aquitaine'
  WHEN dept IN ('69','01','07','26','38','42','43','63','73','74','15') THEN 'Auvergne-Rhône-Alpes'
  WHEN dept IN ('44','49','53','72','85') THEN 'Pays de la Loire'
  WHEN dept IN ('35','22','29','56') THEN 'Bretagne'
  WHEN dept IN ('59','62') THEN 'Hauts-de-France'
  WHEN dept IN ('02','60','80') THEN 'Hauts-de-France'
  WHEN dept IN ('67','68') THEN 'Grand Est'
  WHEN dept IN ('54','55','57','88','08','10','51','52') THEN 'Grand Est'
  WHEN dept IN ('76','27') THEN 'Normandie'
  WHEN dept IN ('14','50','61') THEN 'Normandie'
  WHEN dept IN ('45','28','36','37','41','18') THEN 'Centre-Val de Loire'
  WHEN dept IN ('21','25','39','58','70','71','89','90') THEN 'Bourgogne-Franche-Comté'
  WHEN dept IN ('2A','2B','20') THEN 'Corse'
  ELSE NULL
END
WHERE region IS NULL AND dept IS NOT NULL;


-- ────────────────────────────────────────────────────────────
-- 2. Table submissions (messages envoyés)
-- ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID NOT NULL REFERENCES landscapers(id),

    sequence_step INT NOT NULL,
    message_variant TEXT,
    channel TEXT NOT NULL CHECK (channel IN ('contact_form', 'email')),

    message_sent TEXT NOT NULL,
    subject_sent TEXT,
    sender_name TEXT,
    sender_email TEXT,

    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending',
        'in_progress',
        'success',
        'failed_no_form',
        'failed_captcha',
        'failed_mapping',
        'failed_submit',
        'failed_timeout',
        'failed_blocked',
        'failed_other',
        'skipped'
    )),
    error_details TEXT,

    proxy_ip TEXT,
    page_load_time_ms INT,
    form_fill_time_ms INT,
    screenshot_path TEXT,

    attempted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_submissions_prospect ON submissions(prospect_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_step ON submissions(sequence_step, message_variant);

-- RLS
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY submissions_auth_select ON submissions FOR SELECT TO authenticated USING (true);
CREATE POLICY submissions_auth_insert ON submissions FOR INSERT TO authenticated WITH CHECK (true);
CREATE POLICY submissions_auth_update ON submissions FOR UPDATE TO authenticated USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────
-- 3. Table responses (réponses reçues)
-- ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID REFERENCES landscapers(id),
    submission_id UUID REFERENCES submissions(id),

    response_channel TEXT NOT NULL CHECK (response_channel IN ('email', 'phone', 'form', 'other')),
    response_body TEXT,
    response_subject TEXT,

    sentiment TEXT CHECK (sentiment IN ('positive', 'neutral', 'negative', 'opt_out')),
    intent TEXT CHECK (intent IN ('demo_request', 'question', 'not_interested', 'stop', 'other')),
    notes TEXT,

    received_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE responses ENABLE ROW LEVEL SECURITY;
CREATE POLICY responses_auth_select ON responses FOR SELECT TO authenticated USING (true);
CREATE POLICY responses_auth_insert ON responses FOR INSERT TO authenticated WITH CHECK (true);
CREATE POLICY responses_auth_update ON responses FOR UPDATE TO authenticated USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────
-- 4. Table call_log (journal d'appels)
-- ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS call_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID NOT NULL REFERENCES landscapers(id),

    called_at TIMESTAMPTZ DEFAULT now(),
    duration_seconds INT,
    outcome TEXT NOT NULL CHECK (outcome IN (
        'answered_interested',
        'answered_not_interested',
        'answered_callback',
        'voicemail',
        'no_answer',
        'wrong_number',
        'demo_booked'
    )),
    notes TEXT,
    next_action TEXT,
    next_action_date DATE,

    created_at TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE call_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY call_log_auth_select ON call_log FOR SELECT TO authenticated USING (true);
CREATE POLICY call_log_auth_insert ON call_log FOR INSERT TO authenticated WITH CHECK (true);


-- ────────────────────────────────────────────────────────────
-- 5. Table campaign_config (config clé/valeur)
-- ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS campaign_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

INSERT INTO campaign_config (key, value) VALUES
  ('daily_send_limit', '50'),
  ('delay_between_sends_min', '60'),
  ('delay_between_sends_max', '120'),
  ('days_between_steps', '4'),
  ('days_before_call', '2'),
  ('send_days', '["monday", "wednesday", "friday"]'),
  ('send_hour_start', '8'),
  ('send_hour_end', '10'),
  ('active_variants', '["A", "B"]'),
  ('sender_name', '"Quentin"'),
  ('sender_email', '"devis@vao-solution.com"')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE campaign_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY config_auth_select ON campaign_config FOR SELECT TO authenticated USING (true);
CREATE POLICY config_auth_update ON campaign_config FOR UPDATE TO authenticated USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────
-- 6. Vues analytics
-- ────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_variant_performance AS
SELECT
    s.message_variant,
    s.sequence_step,
    COUNT(*) as total_sent,
    COUNT(*) FILTER (WHERE s.status = 'success') as successful,
    COUNT(r.id) as responses,
    COUNT(r.id) FILTER (WHERE r.sentiment = 'positive') as positive_responses,
    COUNT(r.id) FILTER (WHERE r.intent = 'demo_request') as demo_requests,
    ROUND(
      COUNT(r.id)::numeric /
      NULLIF(COUNT(*) FILTER (WHERE s.status = 'success'), 0) * 100, 2
    ) as response_rate
FROM submissions s
LEFT JOIN responses r ON r.submission_id = s.id
GROUP BY s.message_variant, s.sequence_step
ORDER BY s.sequence_step, s.message_variant;

CREATE OR REPLACE VIEW v_daily_stats AS
SELECT
    DATE(s.attempted_at) as day,
    COUNT(*) as attempts,
    COUNT(*) FILTER (WHERE s.status = 'success') as successes,
    COUNT(*) FILTER (WHERE s.status LIKE 'failed%') as failures,
    ROUND(
      COUNT(*) FILTER (WHERE s.status = 'success')::numeric /
      NULLIF(COUNT(*), 0) * 100, 1
    ) as success_rate,
    COUNT(DISTINCT s.proxy_ip) as proxies_used
FROM submissions s
WHERE s.attempted_at IS NOT NULL
GROUP BY DATE(s.attempted_at)
ORDER BY day DESC;

CREATE OR REPLACE VIEW v_call_list_today AS
SELECT
    p.id,
    p.prenom_gerant,
    p.nom_gerant,
    p.company_name,
    p.phone,
    p.city,
    p.website,
    p.outreach_score,
    p.activity_types,
    s.message_sent as last_message,
    s.completed_at as last_sent_at
FROM landscapers p
JOIN submissions s ON s.prospect_id = p.id AND s.status = 'success'
WHERE p.tier = 1
  AND p.next_action_type = 'call'
  AND p.next_action_date <= CURRENT_DATE
  AND p.campaign_status NOT IN ('opted_out', 'not_interested', 'demo_booked', 'converted')
ORDER BY p.outreach_score DESC;

CREATE OR REPLACE VIEW v_pipeline AS
SELECT
    campaign_status,
    tier,
    COUNT(*) as count,
    AVG(outreach_score) as avg_score
FROM landscapers
WHERE campaign_status IS NOT NULL AND campaign_status != 'new'
GROUP BY campaign_status, tier
ORDER BY tier, campaign_status;
