# Architecture — geo-leaad-fr-landscaping

## Vue d'ensemble

```
GitHub (code source)
    ↓ git pull
Serveur VPS (exécution des agents Python)
    ↓ asyncpg / SQLAlchemy
Supabase (PostgreSQL — base de données)
    ↑ Supabase JS SDK
Lovable / CRM (interface web — crm/index.html)
```

---

## Structure des fichiers

### Répertoire racine

| Fichier | Rôle |
|---|---|
| `.env` | Variables d'environnement locales (non committé) |
| `.env.example` | Modèle `.env` à copier pour configurer le projet |
| `requirements.txt` | Dépendances Python (`playwright`, `sqlalchemy`, `fastapi`, `asyncpg`…) |
| `README.md` | Guide d'installation et d'utilisation |
| `ARCHITECTURE.md` | Ce fichier |
| `setup_projet.ps1` | Script PowerShell d'installation sur Windows |

### `scripts/` — Agents Python

| Fichier | Rôle |
|---|---|
| `config.py` | Chargement `.env`, constantes globales (`DATABASE_URL`, `HEADLESS`, délais…) |
| `models.py` | Modèle SQLAlchemy `Landscaper` (table `landscapers`) |
| `init_db.py` | Création des tables en base — **lancer une seule fois** |
| `test_connection.py` | Test de connexion PostgreSQL — diagnostic rapide |
| `scraper.py` | Scraper manuel interactif par département |
| `scrapers.py` | Fonctions partagées : Playwright, extraction fiches, détection blocage |
| `scraper_fiche.py` | Extraction détaillée d'une fiche Google Maps (téléphone, site, email…) |
| `scheduler.py` | **Agent principal** : grille géographique 0.3°×0.3°, scrape toute la France automatiquement |
| `clean_leads.py` | Nettoyage des doublons et données manquantes |
| `enrich_leads.py` | Enrichissement : gérant, SIRET, forme juridique (via API externe) |
| `debug_maps.py` | Outil de débogage Playwright pour inspecter les pages Google Maps |
| `grid_tasks.json` | **État de progression** du scraping par cellule géographique (auto-généré) |

### `api/` — API FastAPI

| Fichier | Rôle |
|---|---|
| `main.py` | API REST FastAPI : contrôle des agents (start/stop/status), accès aux leads et stats |
| `start.sh` | Script bash pour lancer l'API en production avec uvicorn |

### `crm/` — Interface CRM

| Fichier | Rôle |
|---|---|
| `index.html` | CRM complet en HTML/JS : liste des leads, filtres, changement de statut, notes, rappels, export CSV |
| `migration.sql` | Migrations SQL à exécuter dans Supabase : ajout colonnes CRM, index, RLS, fonctions |
| `vercel.json` | Configuration de déploiement Vercel pour le CRM |

### `logs/` — Journaux

Créé automatiquement par l'API. Contient `scheduler.log`, `clean_leads.log`, `enrich_leads.log`.

---

## Ordre de lancement

### 1. Installation (une seule fois)

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium
cp .env.example .env
# Remplir .env avec DATABASE_URL et API_KEY
```

### 2. Initialisation de la base (une seule fois)

```bash
python scripts/test_connection.py   # vérifie la connexion
python scripts/init_db.py           # crée la table landscapers
# Puis exécuter crm/migration.sql dans Supabase Dashboard → SQL Editor
```

### 3. Lancement de l'API

```bash
bash api/start.sh          # port 8000 par défaut
# ou
bash api/start.sh 8080     # port personnalisé
```

### 4. Lancement du scraper (via API ou directement)

```bash
# Via l'API (recommandé — piloté depuis Lovable)
curl -X POST http://localhost:8000/agents/scraper/start \
  -H "X-Api-Key: VOTRE_CLE"

# Directement en ligne de commande
python scripts/scheduler.py              # reprend la progression
python scripts/scheduler.py --reset     # repart de zéro
python scripts/scheduler.py --stats     # stats sans scraper
python scripts/scheduler.py --missing   # zones sans leads uniquement
```

### 5. Nettoyage et enrichissement

```bash
python scripts/clean_leads.py    # à lancer après chaque batch de scraping
python scripts/enrich_leads.py   # enrichissement SIRET/gérant
```

---

## Connexions entre composants

### Variables d'environnement (`.env`)

```
DATABASE_URL=postgresql+asyncpg://...   → utilisé par scripts/ et api/
API_KEY=...                              → authentification des appels à l'API FastAPI
HEADLESS=true                            → Playwright (true = sans fenêtre)
MIN_DELAY / MAX_DELAY                    → délais entre requêtes Google Maps
```

### Flux de données

```
scheduler.py
  → Playwright scrape Google Maps
  → SQLAlchemy INSERT INTO landscapers (via DATABASE_URL)
  → Supabase PostgreSQL

api/main.py
  → POST /agents/scraper/start  → lance scheduler.py en subprocess
  → GET  /leads                 → asyncpg SELECT FROM landscapers
  → GET  /stats                 → asyncpg COUNT / GROUP BY statut

crm/index.html
  → Supabase JS SDK (connexion directe à Supabase)
  → SELECT landscapers (lecture des leads)
  → UPDATE landscapers (statut, notes, rappel_le, assigne_a)
  → Realtime subscriptions (mises à jour en direct)
```

### Authentification

| Composant | Mécanisme |
|---|---|
| API FastAPI | Header `X-Api-Key` (valeur dans `.env → API_KEY`) |
| CRM Lovable | Supabase Auth (email/mot de passe) + RLS PostgreSQL |
| Scraper Python | `service_role` Supabase (bypass RLS, dans `DATABASE_URL`) |

---

## Valeurs autorisées pour le champ `statut`

Le CRM utilise ces valeurs exactes (sans accents) :

| Valeur en base | Label affiché |
|---|---|
| `nouveau` | Nouveau |
| `contacte` | Contacté |
| `interesse` | Intéressé |
| `client` | Client |
| `perdu` | Perdu |

> **Important** : si une contrainte CHECK existe sur la colonne `statut` dans Supabase,
> elle doit autoriser exactement ces 5 valeurs. Voir section "Bug statut" ci-dessous.

---

## Bug connu : erreur de contrainte sur le statut

**Symptôme** : `new row for relation "landscapers" violates check constraint "landscapers_statut_check"`

**Cause** : Une contrainte CHECK a été créée sur la colonne `statut` (probablement via Lovable)
avec des valeurs différentes de celles utilisées par le CRM.

**Correction** — exécuter dans Supabase Dashboard → SQL Editor :

```sql
-- Vérifier les valeurs actuelles de la contrainte
SELECT pg_get_constraintdef(c.oid)
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
WHERE t.relname = 'landscapers' AND c.conname = 'landscapers_statut_check';

-- Supprimer l'ancienne contrainte et la recréer avec les bonnes valeurs
ALTER TABLE landscapers DROP CONSTRAINT landscapers_statut_check;
ALTER TABLE landscapers ADD CONSTRAINT landscapers_statut_check
  CHECK (statut IN ('nouveau', 'contacte', 'interesse', 'client', 'perdu'));
```
