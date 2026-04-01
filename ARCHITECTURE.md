# Architecture — geo-leaad-fr-landscaping

## Vue d'ensemble

```
GitHub (code source)
    ↓ git pull (geoleaad-sync.service — boucle 5 min)
VPS Ubuntu 22.04 — 178.104.104.36
    ├── nginx (443/80) → reverse proxy → :8000
    ├── geoleaad-api.service  → uvicorn → FastAPI (api/main.py)
    ├── geoleaad-sync.service → git_sync.sh (boucle infinie)
    └── scheduler.py (processus user — 50 leads/jour)
         ↕
    Supabase PostgreSQL (asyncpg / Supabase JS SDK)
         ↑
    CRM (crm/index.html — déployé sur Vercel)
```

---

## Infrastructure serveur

**VPS** : Ubuntu 22.04, 4 Go RAM, Hetzner (nbg1)
**IP** : 178.104.104.36
**URL publique** : https://178-104-104-36.sslip.io
**SSL** : Let's Encrypt (certbot, renouvellement automatique)
**Uptime moyen** : depuis le 23 mars 2026

### Services systemd

| Service | État | Rôle |
|---|---|---|
| `geoleaad-api.service` | enabled / running | FastAPI via uvicorn, port 8000 |
| `geoleaad-sync.service` | enabled / running | git pull automatique toutes les 5 min |
| `nginx.service` | enabled / running | Reverse proxy HTTPS → :8000 |

### Nginx

- Écoute sur 443 (HTTPS) et 80 (redirect)
- `server_name`: 178-104-104-36.sslip.io
- Proxy vers `http://127.0.0.1:8000`
- Timeouts : 60s connect, 120s send/read
- Logs : `/var/log/nginx/geoleaad-api.{access,error}.log`

### Ports ouverts

| Port | Service |
|---|---|
| 22 | SSH |
| 80 | HTTP (redirect HTTPS) |
| 443 | HTTPS (nginx) |
| 8000 | FastAPI (**localhost uniquement** — plus exposé externalement) |

---

## Structure des fichiers

### Répertoire racine

| Fichier | Rôle |
|---|---|
| `.env` | Variables d'environnement production (non committé) |
| `.env.example` | Modèle `.env` à copier |
| `requirements.txt` | Dépendances Python |
| `README.md` | Guide d'installation et d'utilisation |
| `ARCHITECTURE.md` | Ce fichier |
| `git_sync.sh` | Script de synchronisation GitHub automatique (toutes les 5 min) |

### `scripts/` — Agents Python

| Fichier | Rôle |
|---|---|
| `config.py` | **Config centrale** : tous les `os.getenv()`, validation au démarrage, re-exporte `get_logger` |
| `logger.py` | **Logs centralisés** : `get_logger(name)`, fichier unique `logs/app.log` (10 MB × 5) |
| `models.py` | Modèle SQLAlchemy `Landscaper` (table `landscapers`) |
| `init_db.py` | Création des tables — **lancer une seule fois** |
| `test_connection.py` | Test de connexion PostgreSQL |
| `scheduler.py` | **Agent principal** : grille 0.3°×0.3°, scraping Google Maps |
| `scraper_core.py` | **Helpers Playwright partagés** : extraction fiches, email, rating, scroll, CAPTCHA |
| `clean_leads.py` | Classification 3 niveaux des leads (positif / hors-cible / NAF) |
| `enrich.py` | **Enrichissement unifié** : societe.com (Playwright) → API gouvernementale (fallback) |
| `debug_maps.py` | Outil de débogage Playwright (usage ponctuel) |
| `grid_tasks.json` | **État de progression** du scraping (auto-généré, modifié en continu) |

### `api/` — API FastAPI

| Fichier | Rôle |
|---|---|
| `main.py` | API REST FastAPI : start/stop/status agents, accès leads et stats |
| `start.sh` | Lance uvicorn en production |

### `crm/` — Interface CRM

| Fichier | Rôle |
|---|---|
| `index.html` | CRM HTML/JS : leads, filtres, pipeline 16 statuts, notes, rappels, export CSV |
| `migration.sql` | Migrations SQL Supabase (colonnes CRM, index, RLS, fonctions) |
| `vercel.json` | Config déploiement Vercel |

### `logs/` — Journaux

| Fichier | Rôle |
|---|---|
| `app.log` | **Log unique** de tous les scripts (rotation 10 MB × 5) |
| `git_sync.log` | Logs du script de sync GitHub |

---

## Flux de données

```
scheduler.py
  → Playwright scrape Google Maps (Chromium headless)
  → SQLAlchemy INSERT INTO landscapers (asyncpg → DATABASE_URL)
  → Supabase PostgreSQL

api/main.py
  → POST /agents/scraper/start  → lance scheduler.py en subprocess
  → GET  /leads                 → asyncpg SELECT FROM landscapers
  → GET  /stats                 → asyncpg COUNT / GROUP BY statut

crm/index.html (Vercel)
  → Supabase JS SDK (connexion directe)
  → SELECT landscapers (lecture leads, pagination 50/page)
  → UPDATE landscapers (statut, notes, rappel_le, assigne_a)
  → Realtime subscriptions

git_sync.sh (geoleaad-sync.service)
  → git fetch origin main → compare HEAD vs origin/main
  → git pull si nouveaux commits
  → systemctl restart geoleaad-api si api/ ou requirements.txt changés
  → JAMAIS restart automatique de scheduler.py (intervention manuelle requise)
```

---

## Authentification

| Composant | Mécanisme |
|---|---|
| API FastAPI | Header `X-Api-Key` (valeur dans `.env → API_KEY`) |
| CRM Lovable | Supabase Auth (email/mot de passe) + RLS PostgreSQL |
| Scraper Python | `service_role` Supabase (bypass RLS, dans `DATABASE_URL`) |

---

## Pipeline CRM — 16 statuts

| Valeur en base | Label affiché | Phase |
|---|---|---|
| `nouveau` | Nouveau | Entrant |
| `hors_cible` | Hors cible | Qualification |
| `ferme` | Fermé | Qualification |
| `a_contacter` | À contacter | Qualification |
| `pas_encore_approche` | Pas encore approché | Qualification |
| `contacte` | Contacté | Approche |
| `premier_message` | 1er msg — sans réponse | Approche |
| `relance` | Relancé | Approche |
| `en_discussion` | En discussion | Négociation |
| `demo_planifiee` | Démo planifiée | Négociation |
| `demo_faite` | Démo faite | Négociation |
| `offre_envoyee` | Offre envoyée | Closing |
| `gagne` | Gagné | Closing |
| `perdu` | Perdu | Closing |
| `sans_suite` | Sans suite | Archivé |
| `trop_tot` | Trop tôt | Archivé |

> **Important** : toute contrainte CHECK sur `statut` dans Supabase doit autoriser ces 16 valeurs exactes.

---

## Variables d'environnement (`.env`)

```
DATABASE_URL=postgresql+asyncpg://...   → asyncpg (scripts/ et api/)
API_KEY=...                              → authentification API FastAPI
HEADLESS=true                            → Playwright (true = sans fenêtre)
SEARCH_TERM=paysagistes                  → terme de recherche Google Maps
MIN_DELAY / MAX_DELAY                    → délais entre requêtes (secondes)
LOG_LEVEL=INFO                           → niveau de log
```

---

## Commandes de gestion production

```bash
# Services
systemctl status geoleaad-api geoleaad-sync
systemctl restart geoleaad-api
journalctl -fu geoleaad-api
tail -f /opt/geo-leaad-fr-landscaping/logs/git_sync.log

# Scraper (gestion manuelle)
ps aux | grep scheduler.py
pkill -f "scheduler.py"
nohup python scripts/scheduler.py > logs/scheduler_nohup.log 2>&1 &

# API (test)
curl -H "X-Api-Key: VOTRE_CLE" https://178-104-104-36.sslip.io/stats

# Nettoyage / enrichissement
source .venv/bin/activate
python scripts/clean_leads.py
python scripts/enrich.py
```

---

## Bug connu : contrainte CHECK sur statut

**Symptôme** : `new row for relation "landscapers" violates check constraint "landscapers_statut_check"`

**Cause** : Contrainte CHECK créée avec un jeu de valeurs incomplet (avant le pipeline 16 statuts).

**Correction** — dans Supabase Dashboard → SQL Editor :

```sql
ALTER TABLE landscapers DROP CONSTRAINT IF EXISTS landscapers_statut_check;
ALTER TABLE landscapers ADD CONSTRAINT landscapers_statut_check
  CHECK (statut IN (
    'nouveau','hors_cible','ferme','a_contacter','pas_encore_approche',
    'contacte','premier_message','relance','en_discussion',
    'demo_planifiee','demo_faite','offre_envoyee',
    'gagne','perdu','sans_suite','trop_tot'
  ));
```
