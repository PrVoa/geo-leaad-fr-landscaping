# geo-leaad-fr-landscaping

Scraper automatique Google Maps pour extraire des leads paysagistes en France.
Construit avec Playwright + FastAPI + SQLAlchemy async + PostgreSQL (Supabase).

**Production** : `https://178-104-104-36.sslip.io` — VPS Ubuntu, nginx + SSL Let's Encrypt

---

## Architecture rapide

```
GitHub (code source)
    ↓ git pull automatique (geoleaad-sync.service — toutes les 5 min)
VPS Ubuntu — /opt/geo-leaad-fr-landscaping/
    ├── scheduler.py     ← agent scraping (50 leads/jour, Playwright)
    ├── api/main.py      ← FastAPI (geoleaad-api.service, port 8000)
    └── crm/index.html   ← CRM web (déployé sur Vercel)
         ↕ asyncpg / Supabase JS SDK
Supabase (PostgreSQL — base de données)
```

---

## Services systemd (production)

| Service | Rôle | Commande |
|---|---|---|
| `geoleaad-api.service` | FastAPI — port 8000 (nginx proxy) | `systemctl restart geoleaad-api` |
| `geoleaad-sync.service` | Git pull automatique toutes les 5 min | `systemctl status geoleaad-sync` |

```bash
# État des services
systemctl status geoleaad-api geoleaad-sync

# Logs en temps réel
journalctl -fu geoleaad-api
tail -f logs/git_sync.log

# Redémarrer l'API manuellement
systemctl restart geoleaad-api

# Redémarrer le scraper manuellement (JAMAIS auto via sync)
pkill -f "scheduler.py" && nohup python scripts/scheduler.py > logs/scheduler_nohup.log 2>&1 &
```

---

## Structure des fichiers

```
scripts/
  config.py           ← chargement .env, constantes globales
  models.py           ← modèle SQLAlchemy Landscaper
  init_db.py          ← création des tables (1 seule fois)
  test_connection.py  ← test connexion PostgreSQL
  scheduler.py        ← AGENT PRINCIPAL : grille 0.3°×0.3°, 50 leads/jour
  scrapers.py         ← fonctions Playwright partagées
  scraper_fiche.py    ← extraction détaillée d'une fiche Google Maps
  clean_leads.py      ← nettoyage doublons
  enrich_leads.py     ← enrichissement SIRET / gérant (API Entreprise)
  debug_maps.py       ← outil debug Playwright
  grid_tasks.json     ← état de progression du scraping (auto-généré)
api/
  main.py             ← endpoints FastAPI (start/stop/status, leads, stats)
  start.sh            ← lancement uvicorn
crm/
  index.html          ← CRM complet (Supabase JS, Vercel)
  migration.sql       ← migrations SQL à exécuter dans Supabase
  vercel.json         ← config déploiement Vercel
git_sync.sh           ← sync GitHub automatique (piloté par geoleaad-sync)
logs/                 ← journaux applicatifs (auto-créé)
```

---

## Installation (Linux — nouveau serveur)

```bash
# 1. Cloner
git clone https://github.com/PrVOA/geo-leaad-fr-landscaping /opt/geo-leaad-fr-landscaping
cd /opt/geo-leaad-fr-landscaping

# 2. Venv + dépendances
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium

# 3. Configurer
cp .env.example .env
nano .env   # remplir DATABASE_URL, API_KEY

# 4. Initialiser la base
python scripts/test_connection.py
python scripts/init_db.py
# Puis exécuter crm/migration.sql dans Supabase Dashboard → SQL Editor
```

---

## Lancement en développement

```bash
source .venv/bin/activate

# API FastAPI
bash api/start.sh                           # port 8000

# Scraper (modes disponibles)
python scripts/scheduler.py                 # reprend la progression
python scripts/scheduler.py --reset         # repart de zéro
python scripts/scheduler.py --stats         # stats sans scraper
python scripts/scheduler.py --missing       # zones sans leads uniquement

# Nettoyage et enrichissement
python scripts/clean_leads.py
python scripts/enrich_leads.py
```

---

## Base de données (Supabase)

Tables gérées par le projet :
- `landscapers` : leads paysagistes (place_id unique, statut CRM, notes, rappels)
- `grid_tasks` : cellules géographiques de la grille de scraping

Pipeline CRM — 16 statuts :

| Valeur en base | Label affiché |
|---|---|
| `nouveau` | Nouveau |
| `hors_cible` | Hors cible |
| `ferme` | Fermé |
| `a_contacter` | À contacter |
| `pas_encore_approche` | Pas encore approché |
| `contacte` | Contacté |
| `premier_message` | 1er msg — sans réponse |
| `relance` | Relancé |
| `en_discussion` | En discussion |
| `demo_planifiee` | Démo planifiée |
| `demo_faite` | Démo faite |
| `offre_envoyee` | Offre envoyée |
| `gagne` | Gagné |
| `perdu` | Perdu |
| `sans_suite` | Sans suite |
| `trop_tot` | Trop tôt |
