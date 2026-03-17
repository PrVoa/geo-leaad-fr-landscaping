# geo-leaad-fr-landscaping

Scraper automatique Google Maps pour extraire des leads paysagistes en France.
Construit avec Playwright + SQLAlchemy async + PostgreSQL (Supabase).

## Structure

```
scripts/
  init_db.py          <- Cree les tables (a lancer 1 fois)
  test_connection.py  <- Teste la connexion PostgreSQL
  scraper.py          <- Scraper manuel par departement
  scheduler.py        <- 50 paysagistes/jour automatique
requirements.txt
.env.example
```

## Installation Windows (PowerShell)

```powershell
# 0. Debloquer PowerShell (si besoin)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 1. Creer et activer le venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Installer les packages
pip install -r requirements.txt

# 3. Installer Chromium
python -m playwright install --with-deps chromium

# 4. Creer le .env
copy .env.example .env
notepad .env   # remplir DATABASE_URL avec Supabase

# 5. Tester la connexion
python scripts/test_connection.py

# 6. Creer les tables
python scripts/init_db.py

# 7. Lancer le scraper
python scripts/scheduler.py --dept 69
```

## Utilisation

```powershell
# Test de connexion
python scripts/test_connection.py

# Initialiser la base (1 seule fois)
python scripts/init_db.py

# Scraper manuel (interactif)
python scripts/scraper.py

# Scheduler automatique 50/jour
python scripts/scheduler.py --dept 69
```

## Base de donnees (Supabase)

Tables creees par init_db.py :
- landscapers  : paysagistes extraits (place_id unique)
- grid_tasks   : zones geographiques a scraper
