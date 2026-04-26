"""
Migration one-shot : long_term.json → table Supabase `memories`.
Source = 'migration' pour distinguer des données futures.

Usage :
    python openclaw/scripts/migrate_long_term_to_supabase.py
"""
import json, os, sys, requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/opt/openclaw/.env")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
LONG_TERM_FILE = Path("/opt/openclaw/memory/long_term.json")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("SUPABASE_URL ou SUPABASE_KEY manquant dans .env")
    sys.exit(1)

if not LONG_TERM_FILE.exists():
    print(f"{LONG_TERM_FILE} introuvable")
    sys.exit(1)

with open(LONG_TERM_FILE, encoding="utf-8") as f:
    lt = json.load(f)

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

# Mapping catégories JSON → catégories Supabase (singulier)
CAT_MAP = {
    "decisions": "decision",
    "apprentissages": "apprentissage",
    "erreurs": "erreur",
}

rows = []
for json_cat, supabase_cat in CAT_MAP.items():
    entries = lt.get(json_cat, [])
    for entry in entries:
        date_str = entry.get("date", "")
        content = entry.get("contenu", "").strip()
        if not content:
            continue
        # Calcul week_number et month_number depuis la date originale
        try:
            dt = datetime.fromisoformat(date_str)
            week_number = dt.isocalendar()[1]
            month_number = dt.month
        except Exception:
            week_number = None
            month_number = None

        rows.append({
            "created_at": date_str or None,
            "category": supabase_cat,
            "content": content,
            "source": "migration",
            "founder": None,
            "week_number": week_number,
            "month_number": month_number,
            "metadata": {},
        })

print(f"Entrées à migrer : {len(rows)}")
if not rows:
    print("Rien à migrer.")
    sys.exit(0)

# Insert par batch de 50
BATCH = 50
inserted = 0
for i in range(0, len(rows), BATCH):
    batch = rows[i:i + BATCH]
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers=headers,
        json=batch,
        timeout=30,
    )
    if r.status_code in (200, 201):
        inserted += len(batch)
        print(f"  Batch {i // BATCH + 1} : {len(batch)} insérés (total: {inserted})")
    else:
        print(f"  ERREUR batch {i // BATCH + 1} : {r.status_code} {r.text[:300]}")
        sys.exit(1)

print(f"\nMigration terminée : {inserted}/{len(rows)} entrées insérées dans Supabase.")
print(f"long_term.json conservé comme backup ({LONG_TERM_FILE}).")
