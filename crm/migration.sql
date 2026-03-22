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
