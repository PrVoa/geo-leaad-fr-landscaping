#!/usr/bin/env python3
"""
Import de prospects depuis un CSV ou JSON vers Supabase.
Gère le dédoublonnage par SIRET ou (company_name + city).
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.client import get_client

log = logging.getLogger("vao.import")

# Mapping des noms de colonnes CSV vers les colonnes Supabase
COLUMN_MAP = {
    # Noms possibles dans le CSV → nom colonne Supabase (landscapers)
    "company_name": "company_name",
    "nom_entreprise": "company_name",
    "raison_sociale": "company_name",
    "nom": "company_name",
    "owner_name": "nom_gerant",
    "nom_gerant": "nom_gerant",
    "gerant": "nom_gerant",
    "prenom_gerant": "prenom_gerant",
    "prenom": "prenom_gerant",
    "phone": "phone",
    "telephone": "phone",
    "tel": "phone",
    "email": "email",
    "mail": "email",
    "address": "address",
    "adresse": "address",
    "city": "city",
    "ville": "city",
    "postal_code": "postal_code",
    "code_postal": "postal_code",
    "cp": "postal_code",
    "department": "dept",
    "dept": "dept",
    "departement": "dept",
    "website": "website",
    "site_web": "website",
    "site": "website",
    "url": "website",
    "siret": "siret",
    "naf_code": "naf_code",
    "code_naf": "naf_code",
    "forme_juridique": "forme_juridique",
}


def _normalize_row(row: dict) -> dict:
    """Normalise une ligne de CSV/JSON vers le schéma Supabase."""
    normalized = {}
    for key, value in row.items():
        key_lower = key.strip().lower().replace(" ", "_")
        target = COLUMN_MAP.get(key_lower)
        if target and value:
            normalized[target] = str(value).strip()

    # Déduire le département du code postal si absent
    if not normalized.get("dept") and normalized.get("postal_code"):
        cp = normalized["postal_code"]
        if len(cp) >= 2:
            normalized["dept"] = cp[:2]

    # Nettoyage basique du site web
    site = normalized.get("website", "")
    if site and not site.startswith("http"):
        normalized["website"] = f"https://{site}"

    return normalized


def _load_csv(path: Path) -> list[dict]:
    """Charge un fichier CSV."""
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        # Essayer aussi avec virgule si pas de colonnes
        if not reader.fieldnames or len(reader.fieldnames) <= 1:
            f.seek(0)
            reader = csv.DictReader(f, delimiter=",")
        for row in reader:
            normalized = _normalize_row(row)
            if normalized.get("company_name"):
                rows.append(normalized)
    return rows


def _load_json(path: Path) -> list[dict]:
    """Charge un fichier JSON (array de dicts)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("data", data.get("results", [data]))
    rows = []
    for item in data:
        normalized = _normalize_row(item)
        if normalized.get("company_name"):
            rows.append(normalized)
    return rows


def import_prospects(file_path: str, batch_size: int = 500, dry_run: bool = False) -> dict:
    """
    Importe des prospects depuis un fichier CSV ou JSON.

    Returns:
        dict avec total, imported, skipped (doublons), errors
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Fichier non trouvé: {path}")

    # Charger les données
    if path.suffix.lower() in (".csv", ".tsv"):
        rows = _load_csv(path)
    elif path.suffix.lower() == ".json":
        rows = _load_json(path)
    else:
        raise ValueError(f"Format non supporté: {path.suffix}")

    log.info("Chargé %d prospects depuis %s", len(rows), path.name)

    if dry_run:
        log.info("Mode dry-run, aucune insertion")
        for r in rows[:5]:
            log.info("  Exemple: %s", r)
        return {"total": len(rows), "imported": 0, "skipped": 0, "errors": 0}

    client = get_client()
    imported = 0
    skipped = 0
    errors = 0

    # Import par batch
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        try:
            result = client.table("landscapers").upsert(
                batch,
                on_conflict="siret",
                ignore_duplicates=True,
            ).execute()
            imported += len(result.data)
            skipped += len(batch) - len(result.data)
        except Exception as e:
            log.error("Erreur batch %d-%d: %s", i, i + len(batch), e)
            # Fallback : insertion ligne par ligne
            for row in batch:
                try:
                    client.table("landscapers").insert(row).execute()
                    imported += 1
                except Exception as row_err:
                    if "duplicate" in str(row_err).lower() or "unique" in str(row_err).lower():
                        skipped += 1
                    else:
                        errors += 1
                        log.warning("Erreur ligne %s: %s", row.get("company_name", "?"), row_err)

        log.info("Batch %d/%d — importés: %d", i // batch_size + 1,
                 (len(rows) + batch_size - 1) // batch_size, imported)

    stats = {"total": len(rows), "imported": imported, "skipped": skipped, "errors": errors}
    log.info("Import terminé: %s", stats)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Import de prospects vers Supabase")
    parser.add_argument("file", help="Chemin du fichier CSV ou JSON")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Taille des batchs d'insertion (défaut: 500)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Lire le fichier sans insérer (vérification)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    stats = import_prospects(args.file, batch_size=args.batch_size, dry_run=args.dry_run)
    print(f"\nTotal: {stats['total']} | Importés: {stats['imported']} | "
          f"Doublons: {stats['skipped']} | Erreurs: {stats['errors']}")


if __name__ == "__main__":
    main()
