-- ============================================================
-- CRM Migration — Ajouter les colonnes CRM à la table landscapers
-- À exécuter dans Supabase Dashboard > SQL Editor
-- ============================================================

-- Colonnes CRM
ALTER TABLE landscapers
  ADD COLUMN IF NOT EXISTS statut      TEXT DEFAULT 'nouveau',
  ADD COLUMN IF NOT EXISTS notes       TEXT,
  ADD COLUMN IF NOT EXISTS rappel_le   DATE,
  ADD COLUMN IF NOT EXISTS assigne_a   TEXT,
  ADD COLUMN IF NOT EXISTS dept        TEXT;

-- Peupler dept depuis address (code postal 5 chiffres → 2 premiers)
UPDATE landscapers
SET dept = SUBSTRING(regexp_replace(address, '^.*?(\d{5}).*$', '\1', 'g'), 1, 2)
WHERE dept IS NULL AND address ~ '\d{5}';

-- Index pour les filtres fréquents
CREATE INDEX IF NOT EXISTS idx_landscapers_statut    ON landscapers(statut);
CREATE INDEX IF NOT EXISTS idx_landscapers_dept      ON landscapers(dept);
CREATE INDEX IF NOT EXISTS idx_landscapers_rappel_le ON landscapers(rappel_le);
CREATE INDEX IF NOT EXISTS idx_landscapers_assigne_a ON landscapers(assigne_a);

-- Fonction pour lister les départements distincts (filtre CRM)
CREATE OR REPLACE FUNCTION crm_depts()
RETURNS TABLE(dept TEXT, nb BIGINT)
LANGUAGE sql STABLE
AS $$
  SELECT dept, COUNT(*) AS nb
  FROM landscapers
  WHERE dept IS NOT NULL
  GROUP BY dept
  ORDER BY dept;
$$;

-- Fonction pour lister les assignés distincts (filtre CRM)
CREATE OR REPLACE FUNCTION crm_assignes()
RETURNS TABLE(assigne_a TEXT)
LANGUAGE sql STABLE
AS $$
  SELECT DISTINCT assigne_a
  FROM landscapers
  WHERE assigne_a IS NOT NULL AND assigne_a <> ''
  ORDER BY assigne_a;
$$;

-- RLS : accès en lecture/écriture pour les utilisateurs authentifiés
ALTER TABLE landscapers ENABLE ROW LEVEL SECURITY;

-- Lecture
DROP POLICY IF EXISTS crm_anon_select ON landscapers;
DROP POLICY IF EXISTS crm_auth_select ON landscapers;
CREATE POLICY crm_auth_select ON landscapers
  FOR SELECT TO authenticated USING (true);

-- Écriture (mise à jour des champs CRM)
DROP POLICY IF EXISTS crm_anon_update ON landscapers;
DROP POLICY IF EXISTS crm_auth_update ON landscapers;
CREATE POLICY crm_auth_update ON landscapers
  FOR UPDATE TO authenticated USING (true) WITH CHECK (true);

-- Note : le scraper Python utilise le service_role (bypass RLS), pas besoin de policy INSERT/DELETE ici.

-- ============================================================
-- Migration Enrichissement — données entreprise
-- ============================================================

ALTER TABLE landscapers
  ADD COLUMN IF NOT EXISTS prenom_gerant   TEXT,        -- prénom du dirigeant
  ADD COLUMN IF NOT EXISTS nom_gerant      TEXT,        -- nom du dirigeant
  ADD COLUMN IF NOT EXISTS siret           TEXT,        -- SIRET ou SIREN
  ADD COLUMN IF NOT EXISTS forme_juridique TEXT,        -- SARL, EI, SAS…
  ADD COLUMN IF NOT EXISTS date_creation   TEXT;        -- date de création entreprise

CREATE INDEX IF NOT EXISTS idx_landscapers_nom_gerant ON landscapers(nom_gerant);
CREATE INDEX IF NOT EXISTS idx_landscapers_siret      ON landscapers(siret);

-- ============================================================
-- Migration ICP — Qualification paysagistes
-- ============================================================

ALTER TABLE landscapers
  ADD COLUMN IF NOT EXISTS type_activite TEXT,      -- creation | entretien | mixte | inconnu
  ADD COLUMN IF NOT EXISTS score_icp     INTEGER,   -- 0-100
  ADD COLUMN IF NOT EXISTS mots_detectes TEXT;      -- mots-clés ICP détectés

-- Index pour filtrer par score et type dans le CRM
CREATE INDEX IF NOT EXISTS idx_landscapers_score_icp     ON landscapers(score_icp);
CREATE INDEX IF NOT EXISTS idx_landscapers_type_activite ON landscapers(type_activite);

-- ============================================================
-- Migration Statuts CRM — contrainte CHECK à jour
-- ============================================================

-- Normaliser les anciens statuts avant d'appliquer la contrainte
-- NB : 'exclu' était l'ancien label posé par clean_leads pour les hors-cible.
-- 'ferme' était l'ancien label pour les sociétés radiées (a_ferme).
UPDATE landscapers SET statut = 'hors_cible'      WHERE statut = 'exclu';
UPDATE landscapers SET statut = 'a_ferme'         WHERE statut = 'ferme';
UPDATE landscapers SET statut = 'hors_cible'      WHERE statut IN ('trop_tot','pas_encore_approche');
UPDATE landscapers SET statut = 'contacte'        WHERE statut IN ('a_contacter','premier_message');
UPDATE landscapers SET statut = 'en_discussion'   WHERE statut IN ('demo_planifiee','demo_faite');
UPDATE landscapers SET statut = 'solution_envoyee' WHERE statut = 'offre_envoyee';
UPDATE landscapers SET statut = 'relance_essai'   WHERE statut = 'relance';
UPDATE landscapers SET statut = 'nouveau'         WHERE statut IS NULL OR statut NOT IN (
  'nouveau','contacte','en_discussion','solution_envoyee','relance_essai',
  'accompagne','gagne','perdu','pas_interesse','sans_suite','hors_cible','a_ferme'
);

-- Supprimer l'ancienne contrainte puis recréer avec les statuts actuels
ALTER TABLE landscapers DROP CONSTRAINT IF EXISTS landscapers_statut_check;
ALTER TABLE landscapers ADD CONSTRAINT landscapers_statut_check CHECK (statut IN (
  'nouveau',
  'contacte', 'en_discussion', 'solution_envoyee', 'relance_essai', 'accompagne', 'gagne',
  'perdu', 'pas_interesse', 'sans_suite', 'hors_cible', 'a_ferme'
));

-- ============================================================
-- Migration Appels — Journal d'appels de prospection
-- ============================================================

ALTER TABLE landscapers
  ADD COLUMN IF NOT EXISTS appels_horodates   JSONB        DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS nb_tentatives      INTEGER      DEFAULT 0,
  ADD COLUMN IF NOT EXISTS premier_repondu_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS dernier_contact_at TIMESTAMPTZ;

-- Format d'une entrée dans appels_horodates :
-- { "type": "répondu" | "sans_réponse",
--   "ts": "2026-04-26T14:32:00Z",
--   "note": "...",
--   "interet": "chaud" | "tiede" | "froid" | null,
--   "duree_min": 5 }

CREATE INDEX IF NOT EXISTS idx_landscapers_premier_repondu_at ON landscapers(premier_repondu_at);
CREATE INDEX IF NOT EXISTS idx_landscapers_dernier_contact_at ON landscapers(dernier_contact_at DESC);
CREATE INDEX IF NOT EXISTS idx_landscapers_nb_tentatives      ON landscapers(nb_tentatives);

-- ============================================================
-- Fusion : tous les "pas_interesse" deviennent "hors_cible"
-- (le bouton "🚫 Pas intéressé" du panel écrit désormais hors_cible)
-- ============================================================

UPDATE landscapers SET statut = 'hors_cible' WHERE statut = 'pas_interesse';

-- ============================================================
-- Tri : noms commençant par emoji/chiffre/symbole en queue
-- Colonne générée + index → ORDER BY name_sort_key, name fait remonter
-- les vrais noms (lettres) en premier, le reste en fin de liste
-- ============================================================

ALTER TABLE landscapers
  ADD COLUMN IF NOT EXISTS name_sort_key SMALLINT
  GENERATED ALWAYS AS (CASE WHEN name ~ '^[[:alpha:]]' THEN 0 ELSE 1 END) STORED;

CREATE INDEX IF NOT EXISTS idx_landscapers_name_sort_key
  ON landscapers (name_sort_key, name);

-- ============================================================
-- RPC : counts agrégés des chips du CRM (1 query au lieu de 7)
-- Sémantique alignée sur applyQuickFilter() côté JS
-- ============================================================

CREATE OR REPLACE FUNCTION get_crm_counts()
RETURNS json
LANGUAGE sql
SECURITY DEFINER
STABLE
AS $$
  SELECT json_build_object(
    -- "Tous" exclut hors_cible/exclu/pas_interesse (chip dédiée pour ces leads écartés)
    'tous',          COUNT(*) FILTER (WHERE statut IS NULL OR statut NOT IN ('hors_cible','exclu','pas_interesse')),
    'a_appeler',     COUNT(*) FILTER (WHERE nb_tentatives = 0 AND (statut IS NULL OR statut = 'nouveau')),
    'repondu',       COUNT(*) FILTER (WHERE premier_repondu_at IS NOT NULL),
    'sans_reponse',  COUNT(*) FILTER (WHERE nb_tentatives > 0 AND premier_repondu_at IS NULL),
    'rappel',        COUNT(*) FILTER (WHERE rappel_le IS NOT NULL),
    'interesse',     COUNT(*) FILTER (WHERE statut IN ('en_discussion','solution_envoyee','relance_essai','accompagne','gagne')),
    -- "Pas intéressé" couvre maintenant uniquement perdu/sans_suite/a_ferme (pas_interesse fusionné dans hors_cible)
    'pas_interesse', COUNT(*) FILTER (WHERE statut IN ('perdu','sans_suite','a_ferme')),
    -- "Hors cible" inclut désormais pas_interesse (défensif au cas où des leads existent encore avec ce statut)
    'hors_cible',    COUNT(*) FILTER (WHERE statut IN ('hors_cible','exclu','pas_interesse')),
    'avec_contact',  COUNT(*) FILTER (WHERE nom_gerant IS NOT NULL AND nom_gerant <> '' AND phone IS NOT NULL AND phone <> '')
  )
  FROM landscapers;
$$;

GRANT EXECUTE ON FUNCTION get_crm_counts() TO authenticated;

-- ============================================================
-- RPC : analytics agrégés (heatmap + bars par heure & jour)
-- Évite de télécharger toutes les rangées appels_horodates côté client
-- Timezone Europe/Paris pour aligner avec la perception utilisateur (FR)
-- ============================================================

CREATE OR REPLACE FUNCTION get_call_analytics()
RETURNS json
LANGUAGE sql
SECURITY DEFINER
STABLE
AS $$
  WITH expanded AS (
    SELECT
      (call_elem->>'ts')::timestamptz AS ts,
      call_elem->>'type' AS call_type
    FROM landscapers,
         LATERAL jsonb_array_elements(COALESCE(appels_horodates, '[]'::jsonb)) AS call_elem
    WHERE jsonb_typeof(appels_horodates) = 'array'
      AND nb_tentatives > 0
  ),
  enriched AS (
    SELECT
      ts,
      call_type = 'répondu' AS is_ans,
      EXTRACT(HOUR FROM ts AT TIME ZONE 'Europe/Paris')::int AS h,
      ((EXTRACT(DOW FROM ts AT TIME ZONE 'Europe/Paris')::int + 6) % 7) AS wd
    FROM expanded
    WHERE ts IS NOT NULL
  )
  SELECT json_build_object(
    'today_calls', (
      SELECT COUNT(*) FROM enriched
      WHERE ts >= (date_trunc('day', NOW() AT TIME ZONE 'Europe/Paris') AT TIME ZONE 'Europe/Paris')
    ),
    'week_calls', (
      SELECT COUNT(*) FROM enriched
      WHERE ts >= (date_trunc('week', NOW() AT TIME ZONE 'Europe/Paris') AT TIME ZONE 'Europe/Paris')
    ),
    'total_ans',    (SELECT COUNT(*) FROM enriched WHERE is_ans),
    'total_no_ans', (SELECT COUNT(*) FROM enriched WHERE NOT is_ans),
    'by_hour', COALESCE((
      SELECT json_agg(json_build_object('h', h, 'ans', ans, 'no_ans', no_ans) ORDER BY h)
      FROM (
        SELECT h,
               COUNT(*) FILTER (WHERE is_ans)     AS ans,
               COUNT(*) FILTER (WHERE NOT is_ans) AS no_ans
        FROM enriched GROUP BY h
      ) x
    ), '[]'::json),
    'by_day', COALESCE((
      SELECT json_agg(json_build_object('wd', wd, 'ans', ans, 'no_ans', no_ans) ORDER BY wd)
      FROM (
        SELECT wd,
               COUNT(*) FILTER (WHERE is_ans)     AS ans,
               COUNT(*) FILTER (WHERE NOT is_ans) AS no_ans
        FROM enriched GROUP BY wd
      ) x
    ), '[]'::json),
    'heatmap', COALESCE((
      SELECT json_agg(json_build_object('wd', wd, 'h', h, 'ans', ans, 'no_ans', no_ans))
      FROM (
        SELECT wd, h,
               COUNT(*) FILTER (WHERE is_ans)     AS ans,
               COUNT(*) FILTER (WHERE NOT is_ans) AS no_ans
        FROM enriched GROUP BY wd, h
      ) x
    ), '[]'::json)
  );
$$;

GRANT EXECUTE ON FUNCTION get_call_analytics() TO authenticated;
