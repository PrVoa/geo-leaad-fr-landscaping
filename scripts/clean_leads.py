"""
Nettoyage de la table landscapers — classification en 3 niveaux.

NIVEAU 1 — GARDER : nom contient un mot paysagiste positif → statut = 'nouveau'
NIVEAU 2 — EXCLURE : nom contient un mot/pattern d'exclusion fort → statut = 'hors_cible'
NIVEAU 3 — AMBIGU : vérification via code_naf déjà en base
    → code_naf commence par 8130 ou 0161 → garder
    → autre code_naf connu              → exclure
    → pas de code_naf                  → laisser 'nouveau' (bénéfice du doute)

ORDRE DE PRIORITÉ dans classifier_local :
    0. Franchises connues  → exclure (priorité absolue, avant tout mot positif)
    1. Mots positifs       → garder
    2. Exclusions fermes   → exclure
    3. BTP/Nettoyage       → exclure sauf si mot jardin présent

Ne supprime jamais — change uniquement le statut.
Seuls les leads en statut 'nouveau' ou NULL sont traités.

Usage :
    python clean_leads.py              # analyse + mise à jour
    python clean_leads.py --dry-run    # affiche sans modifier
    python clean_leads.py --dept 06    # un département seulement
"""
import argparse
import asyncio
import re
import sys
import unicodedata
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("DATABASE_URL manquant dans .env")

_DB_URL = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").replace(
    "postgres+asyncpg://", "postgresql://"
)

from logger import get_logger
log = get_logger("clean_leads")

# ---------------------------------------------------------------------------
# NIVEAU 1 — Mots paysagistes positifs (garder directement)
# ---------------------------------------------------------------------------

MOTS_GARDER = [
    "paysage", "paysagiste", "paysagisme",
    "jardin", "jardins", "jardinier", "jardinage",
    "espaces verts", "espace vert",
    "elagage", "elagueur",
    "arboriste", "arborist", "arboriculture",
    "verdure", "vegetal",
    "bocage",
    "parcs et jardins", "parc et jardin",
    "esprit vert", "nature verte",
    "cimes",
    "horticulture", "horticole",
    "pepiniere",
    "amenagement paysager",
    "entretien jardin", "creation jardin",
]

# Mots positifs conditionnels : valides seulement si combinés avec un autre mot positif
MOTS_GARDER_CONDITIONNELS = {
    "abattage": ["elagage", "jardin", "paysage", "arbo", "arbres"],
}

# ---------------------------------------------------------------------------
# NIVEAU 2 — Mots d'exclusion
# Tous les mots sont en forme normalisée : lowercase, sans accents, & → espace
# ---------------------------------------------------------------------------

# ── Franchises et réseaux de services à domicile ──
# PRIORITÉ 0 : vérifiées avant tout mot positif (franchises avec "jardinage"
# dans leur description seraient faussement gardées sinon)
FRANCHISES = [
    "azae", "domaliance",
    "maison et services", "maison services",  # "Maison & Services" → "maison services"
    "apef ",
    "axeo services", "axeo ",
    "free dom", "centre services",
    "home services", "generale des services", "vivaservices",
    "domicile clean", "tout a dom", "bien dans sa maison",
    "daniel moquet", "idverde", "id verde",
    "serpe ", "terideal",
    "o2 jardinage", "o2 jardi",
    "age d or services",
    "groupama",
    "familles services",         # "Familles & Services"
    "essentiel domicile",        # "Essentiel & Domicile"
    "confiez-nous", "confiez nous",
    "shiva ",
    "aide a domicile", "aide domicile",
    "garde d enfants", "garde enfants",
    "portage de repas",
    "maintien a domicile",
]

# ── Formation et éducation ──
FORMATION = [
    "lycee", "lycee professionnel",
    "cfa ", "cfppa", "mfr ", "ensp",
    "ecole nationale", "campus",
    "agrocampus", "agrocampus ouest",
    "btp cfa",
    "centre de formation",
    "institut national",
    "insa ",
    "naturapolis",
]

# ── Associations, ESAT, insertion ──
ASSOCIATIONS = [
    "esat ",
    "association solidarite", "association intermediaire",
    "association internationale", "association emploi",
    "adapei", "adcr", "ladapt",
    "passerelles pour l emploi",
    "association cherbourgeoise",
    "fo r e t",       # FO.R.E.T après normalisation
    "association granit",
    "association eclair",
    "association saint roch",
    "graines competences",
]

# ── Intérim et RH ──
INTERIM = [
    "interim", " interim",
    "recrutement",
    "aquila rh", "temporis ",
    "r a s interim", "ras interim",
    "agri-interim", "agri interim",
    "hope interim",
    "rm interim",
    "terra interim",
    "vert l interim",
    "manne emploi",
    "tridentt",
]

# ── Tourisme et loisirs ──
TOURISME = [
    "camping",
    "office de tourisme",
    "planetarium",
    "plage ", "dunes ",
    "aire de jeux",
    "cimetiere",
    "gite ", "gites ",
    "vacances ",
    "loisirs chambery",
    "events et loisirs",
    "libellule evasions",
    "voyages de luxe",
    "balades canons",
    "yelloh",
    "parc des bruyeres",
]

# ── Architecture et décoration d'intérieur ──
ARCHITECTURE = [
    "architecte d interieur",
    "architecture interieur",
    "decoration interieur",
    "studio archi",
    "atelier archi",
    "urbanisme design",
    "mc architecture",
    "bureau d etudes techniques",
    "ingenierie",
    "odetec",
    "caue ",
    "conseil d architecture",
]

# ── Espagnol et étranger ──
ESPAGNOL = [
    "limpiezas", "arquitectos",
    "estudio de arquitectura",
    "servicios y limpiezas",
    "inmobiliaria",
    "riells vacacions",
    "jaam sociedad",
    "blitzclean services",
    "ingelan", "arkos", "nexobau",
    "huw webb",
]

# ── Divers hors-cible ──
DIVERS = [
    "notaire",
    "piscines de france",
    "demenagement",
    "location de nacelles", "location materiel",
    "fls ",
    "beton imprime",
    "valormat", "dispano",
    "natural stone",
    "lippi ",
    "artemat",
    "ozae materiaux",
    "garden park concept",
    "beton pret",
    "office national des forets", "onf ",
    "caisse generale", "cgss", "msa ",
    "aquagaia",
    "stop taupes",
    "centrale depannage",
    "eco nuisibles",
    "desinsectisation", "deratisation",
    "destruction nuisibles", "anti nuisibles",
    "esso turquoise",
]

# ── Nettoyage professionnel pur ──
NETTOYAGE = [
    "nettoyage professionnel", "entreprise de nettoyage",
    "clinitex", "proprete", "vitrerie", "lavage ",
    "pressing ",
    "isor ", "propnet", "nikita nettoyage",
    "foltier nettoyage", "karl nettoyage",
    "cnet nettoyage",
    "clean now", "master net",
    "partenaire service", "edif-propre",
    "activ clean", "jon net",
    "maison net", "nhps",
    "ops ", "phps", "spid anjou",
]

# ── BTP / toiture pur ──
BTP = [
    "couvreur", "toiture",
    "maconnerie sarl", "renovation construction",
    "charpente",
    "etancheite", "ravalement",
    "facade ",
    "platrerie", "carrelage",
    "peinture sarl", "menuiserie",
    "electricite", "plomberie",
    "chauffage sarl", "isolation ",
]

# ---------------------------------------------------------------------------
# Regroupements pour la logique de classification
# ---------------------------------------------------------------------------

# Excluent SAUF si un mot jardin/paysage est présent dans le nom
EXCLUSION_SANS_JARDIN = NETTOYAGE + BTP

# Excluent sans condition (hors franchises déjà en priorité 0)
EXCLUSION_FERME = (
    FORMATION + ASSOCIATIONS + INTERIM + TOURISME
    + ARCHITECTURE + ESPAGNOL + DIVERS
)

# Mots qui "sauvent" un lead des exclusions conditionnelles (BTP/Nettoyage)
MOTS_SAUVETAGE = ["jardin", "jardins", "paysage", "paysagiste", "elagage", "espace vert"]

# ---------------------------------------------------------------------------
# Codes NAF (niveau 3 — vérification en base)
# ---------------------------------------------------------------------------

NAF_GARDER = ("8130", "0161")   # entretien espaces verts, soutien agriculture

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normaliser(texte: str) -> str:
    """Minuscules, sans accents, sans ponctuation superflue, espaces normalisés."""
    t = texte.lower().strip()
    t = unicodedata.normalize("NFD", t)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def contient_un(texte: str, mots: list[str]) -> str | None:
    """Retourne le premier mot trouvé, ou None."""
    for m in mots:
        if m in texte:
            return m
    return None

# ---------------------------------------------------------------------------
# NIVEAU 1 & 2 — Classification locale
# ---------------------------------------------------------------------------

def classifier_local(name: str) -> tuple[str | None, str]:
    """
    Retourne (decision, raison).
    decision : 'garder' | 'exclu' | None (ambigu → niveau 3)
    """
    n = normaliser(name)

    # ── Priorité 0 : franchises connues (avant tout mot positif) ──
    mot = contient_un(n, FRANCHISES)
    if mot:
        return "exclu", f"franchise: '{mot.strip()}'"

    # ── Niveau 1 : mots paysagistes directs ──
    mot = contient_un(n, MOTS_GARDER)
    if mot:
        return "garder", f"mot positif: '{mot}'"

    # Mots conditionnels (ex: abattage seul ne suffit pas)
    for mot_cond, mots_requis in MOTS_GARDER_CONDITIONNELS.items():
        if mot_cond in n and contient_un(n, mots_requis):
            return "garder", f"mot positif conditionnel: '{mot_cond}'"

    # ── Niveau 2 : exclusions fermes sans condition ──
    mot = contient_un(n, EXCLUSION_FERME)
    if mot:
        return "exclu", f"exclusion ferme: '{mot.strip()}'"

    # ── Niveau 2 : exclusions conditionnelles (sauf si mot jardin présent) ──
    mot_excl = contient_un(n, EXCLUSION_SANS_JARDIN)
    if mot_excl:
        mot_salut = contient_un(n, MOTS_SAUVETAGE)
        if mot_salut:
            return "garder", f"mot positif '{mot_salut}' rachète exclusion '{mot_excl.strip()}'"
        return "exclu", f"exclusion conditionnelle: '{mot_excl.strip()}'"

    # ── Niveau 3 : ambigu ──
    return None, "ambigu"

# ---------------------------------------------------------------------------
# NIVEAU 3 — Vérification code NAF en base (sans appel HTTP)
# ---------------------------------------------------------------------------

def classifier_naf(code_naf: str | None) -> tuple[str | None, str]:
    """
    Retourne (decision, raison) selon le code_naf déjà stocké en base.
    decision : 'garder' | 'exclu' | None (pas de NAF → bénéfice du doute)
    """
    if not code_naf:
        return None, "pas de code NAF en base → bénéfice du doute"

    naf = re.sub(r"[^0-9A-Za-z]", "", code_naf).upper()

    if any(naf.startswith(code.replace(".", "")) for code in NAF_GARDER):
        return "garder", f"NAF {naf} → espaces verts/agriculture"

    return "exclu", f"NAF {naf} → hors cible"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(dept_filter: str | None, dry_run: bool):
    conn = await asyncpg.connect(_DB_URL)

    # Créer la colonne code_naf si elle n'existe pas encore
    await conn.execute(
        "ALTER TABLE landscapers ADD COLUMN IF NOT EXISTS code_naf TEXT"
    )

    where_clauses = ["(statut = 'nouveau' OR statut IS NULL)"]
    if dept_filter:
        where_clauses.append(f"dept = '{dept_filter}'")
    where = " AND ".join(where_clauses)

    rows = await conn.fetch(
        f"""SELECT place_id, name, statut, code_naf
            FROM landscapers
            WHERE {where}
            ORDER BY name"""
    )

    total = len(rows)
    prefix = "[DRY-RUN] " if dry_run else ""
    print(f"\n{prefix}Analyse de {total} leads...\n")

    a_garder:  dict[str, str] = {}
    a_exclure: dict[str, str] = {}

    nb_naf_garder = 0
    nb_naf_exclu  = 0
    nb_naf_doute  = 0

    for row in rows:
        name     = row["name"] or ""
        code_naf = row["code_naf"]

        decision, raison = classifier_local(name)

        if decision == "garder":
            a_garder[row["place_id"]] = raison
            log.info("GARDER   %-55s  %s", name[:55], raison)

        elif decision == "exclu":
            a_exclure[row["place_id"]] = raison
            log.info("EXCLU    %-55s  %s", name[:55], raison)

        else:
            # ── Niveau 3 : consulter le code_naf en base ──
            decision_naf, raison_naf = classifier_naf(code_naf)

            if decision_naf == "garder":
                a_garder[row["place_id"]] = raison_naf
                nb_naf_garder += 1
                log.info("NAF→GARDER  %-50s  %s", name[:50], raison_naf)
            elif decision_naf == "exclu":
                a_exclure[row["place_id"]] = raison_naf
                nb_naf_exclu += 1
                log.info("NAF→EXCLU   %-50s  %s", name[:50], raison_naf)
            else:
                nb_naf_doute += 1
                log.debug("NAF→DOUTE   %-50s  %s", name[:50], raison_naf)

    nb_local_garder = len(a_garder) - nb_naf_garder
    nb_local_exclu  = len(a_exclure) - nb_naf_exclu
    nb_ambigus      = nb_naf_garder + nb_naf_exclu + nb_naf_doute

    # ── Résumé ──
    print(f"\n{'─' * 60}")
    print(f"  Total analysé      : {total}")
    print(f"  Gardés  (nv 1&2)   : {nb_local_garder}")
    print(f"  Exclus  (nv 1&2)   : {nb_local_exclu}")
    print(f"  Ambigus (nv 3 NAF) : {nb_ambigus}")
    print(f"    └ NAF → gardés   : {nb_naf_garder}")
    print(f"    └ NAF → exclus   : {nb_naf_exclu}")
    print(f"    └ sans NAF       : {nb_naf_doute}  (laissés 'nouveau')")
    print(f"  Total gardés       : {len(a_garder) + nb_naf_doute}")
    print(f"  Total exclus       : {len(a_exclure)}")
    print(f"{'─' * 60}\n")

    # Aperçu exclusions
    if a_exclure:
        print("Aperçu des 20 premières exclusions :")
        for pid, raison in list(a_exclure.items())[:20]:
            row = next((r for r in rows if r["place_id"] == pid), None)
            nom = (row["name"] if row else pid) or pid
            print(f"  - {nom[:52]:<52}  [{raison}]")
        if len(a_exclure) > 20:
            print(f"  ... et {len(a_exclure) - 20} autres")

    # ── Mise à jour base ──
    if dry_run:
        print("\n[DRY-RUN] Aucune modification effectuée.")
        await conn.close()
        return

    if a_exclure:
        ids_exclu = list(a_exclure.keys())
        for i in range(0, len(ids_exclu), 500):
            batch = ids_exclu[i:i + 500]
            await conn.execute(
                "UPDATE landscapers SET statut = 'hors_cible' WHERE place_id = ANY($1::text[])",
                batch,
            )
        print(f"{len(a_exclure)} leads mis à jour → statut='hors_cible'")
    else:
        print("Aucun lead à exclure.")

    await conn.close()


def main():
    parser = argparse.ArgumentParser(description="Nettoyage des leads paysagistes (3 niveaux)")
    parser.add_argument("--dept",    metavar="XX", help="Limite à un département (ex: 06)")
    parser.add_argument("--dry-run", action="store_true", help="Analyse sans modifier la base")
    args = parser.parse_args()

    asyncio.run(run(args.dept, args.dry_run))


if __name__ == "__main__":
    main()
