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
