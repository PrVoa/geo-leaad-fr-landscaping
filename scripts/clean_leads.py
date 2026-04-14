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
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncpg
from config import DB_URL as _DB_URL, get_logger

log = get_logger("clean_leads")

# ============================================================================
# CLASSIFICATION PAR PATTERNS (regex compilées)
# ============================================================================
#
# Toutes les regex matchent sur le nom NORMALISÉ : lowercase, sans accents,
# ponctuation remplacée par des espaces, espaces collapsés.
# Ne PAS écrire d'accents dans les patterns (ils ont été retirés).
#
# Pour ajouter un nouveau cas hors-cible :
#   1. Trouver la catégorie qui colle le mieux dans EXCLUSION_PATTERNS
#   2. Ajouter ton motif dans la regex existante (alternation `|`)
#   3. Ou créer une nouvelle entrée si c'est une catégorie inédite
#
# Pour les exclusions "rachetables" (BTP/nettoyage qui peuvent être un vrai
# paysagiste si "jardin/paysage" est présent), utiliser EXCLUSION_SAUVABLES.
# ============================================================================

# ── NIVEAU 1 : mots positifs (paysagiste FR) ───────────────────────────────
GARDER_RE = re.compile(
    r"\b("
    r"paysag(e|es|er|ere|ers|eres|iste|istes|isme)"
    r"|jardin(s|ier|iere|iers|ieres|age|ages)?"
    r"|espaces?\s+verts?"
    r"|elag(age|ages|ueur|ueurs|ueuse|ueuses)"
    r"|arborist(e|es)|arboriculture|arboricole"
    r"|verdure|vegetal(e|es|aux)?"
    r"|bocage(s)?|cimes?"
    r"|horticult(eur|rice|ure|urale|urales)|horticole(s)?"
    r"|pepini(ere|eres|eriste|eristes)"
    r"|amenagement(s)?\s+paysager(s)?"
    r"|entretien\s+(de\s+)?jardin(s)?"
    r"|creation\s+(de\s+)?jardin(s)?"
    r"|parcs?\s+et\s+jardins?"
    r"|esprit\s+vert|nature\s+verte"
    r")\b",
    re.IGNORECASE,
)

# Mot positif conditionnel : "abattage" ne suffit pas seul
ABATTAGE_RE = re.compile(r"\babattage\b", re.IGNORECASE)
ABATTAGE_CONTEXT_RE = re.compile(
    r"\b(elagage|jardin|paysage|arbor|arbres?)\b", re.IGNORECASE
)

# ── PRIORITÉ 0b : franchises et marques (substring littéral) ───────────────
# Ces marques ne se prêtent pas à une regex (mots commerciaux courts qui
# pourraient déclencher faux positifs). Liste maintenue à plat.
FRANCHISES_LITERAL = [
    "azae", "domaliance",
    "maison et services", "maison services",
    "apef ", "axeo services", "axeo ",
    "free dom", "centre services",
    "home services", "generale des services", "vivaservices",
    "domicile clean", "tout a dom", "bien dans sa maison",
    "daniel moquet", "idverde", "id verde",
    "serpe ", "terideal",
    "o2 jardinage", "o2 jardi",
    "age d or services",
    "groupama",
    "familles services", "essentiel domicile",
    "confiez-nous", "confiez nous",
    "shiva ",
    "aide a domicile", "aide domicile",
    "garde d enfants", "garde enfants",
    "portage de repas", "maintien a domicile",
]

# ── NIVEAU 2 : exclusions fermes par catégorie (regex) ─────────────────────
EXCLUSION_PATTERNS: dict[str, re.Pattern] = {

    # Administration publique, collectivités, services régaliens
    "administration": re.compile(
        r"\b("
        r"mair(ie|ies)|commune\s+de|communaute(s)?\s+de\s+communes?|"
        r"departement\s+de|region\s+de|prefecture|sous[\s-]prefecture|"
        r"ministere|conseil\s+(general|regional|departemental|municipal)|"
        r"caisse\s+(des\s+depots|primaire|nationale|generale)|cgss|msa\b|"
        r"cch?u|chu\b|hopital|polyclinique|"
        r"ehpad|maison\s+de\s+retraite|"
        r"tresorerie|tribunal|gendarmerie|commissariat|"
        r"sdis|samu|pompiers|protection\s+civile"
        r")\b",
        re.IGNORECASE,
    ),

    # Éducation, formation, recherche
    "education_formation": re.compile(
        r"\b("
        r"lycee(s)?|college(s)?|"
        r"ecole(s)?(\s+(primaire|maternelle|secondaire|technique|nationale|d|du|de|des))?|"
        r"groupe\s+scolaire|"
        r"universite(s)?|faculte(s)?|"
        r"cfa\b|cfppa|mfr\b|ensp|insa\b|naturapolis|agrocampus|agricampus|"
        r"centre(s)?\s+de\s+formation|institut\s+(national|universitaire|de\s+formation)|"
        r"campus|btp\s+cfa"
        r")\b",
        re.IGNORECASE,
    ),

    # Associations, ESAT, fondations, congrégations
    "association_insertion": re.compile(
        r"\b("
        r"association(s)?|asbl|"
        r"esat|adapei|adcr|ladapt|graines?\s+competences?|"
        r"fondation|congregation|prieure|"
        r"comite\s+(departemental|regional|local|national)|"
        r"federation|union\s+(des|nationale|locale|sportive|departementale)|"
        r"passerelle(s)?\s+pour\s+l\s?emploi|"
        r"manne\s+emploi|fo\s+r\s+e\s+t"
        r")\b",
        re.IGNORECASE,
    ),

    # Intérim et RH
    "interim_rh": re.compile(
        r"\b("
        r"interim|recrutement|"
        r"aquila\s+rh|temporis|r\s?a\s?s\s+interim|ras\s+interim|"
        r"agri[\s-]?interim|hope\s+interim|rm\s+interim|terra\s+interim|"
        r"vert\s+l\s+interim|tridentt"
        r")\b",
        re.IGNORECASE,
    ),

    # Tourisme, hôtellerie, loisirs, zoos
    "tourisme_loisirs": re.compile(
        r"\b("
        r"camping(s)?|hotel(s|lerie)?|gite(s)?|chambre(s)?\s+d\s?hote(s)?|"
        r"office\s+(de\s+)?tourisme|syndicat\s+d\s?initiative|"
        r"planetarium|musee(s)?|aquarium|"
        r"zoo|zoologique|zoologico|zoological|parc\s+animalier|"
        r"yelloh|pierre\s+et\s+vacances|center\s+parcs?|"
        r"village\s+de\s+vacances|centre\s+de\s+(loisirs|vacances)|"
        r"events?\s+et\s+loisirs|loisirs\s+chambery|"
        r"libellule\s+evasions?|voyages?\s+de\s+luxe|balades?\s+canons?|"
        r"parc\s+des\s+bruyeres|"
        r"discotheque|boite\s+de\s+nuit|cinema|theatre|"
        r"club\s+(de\s+|sportif|nautique|equestre)"
        r")\b",
        re.IGNORECASE,
    ),

    # Lieux/POI géographiques en PRÉFIXE de nom (anchored ^)
    # Très efficace contre les entrées du référentiel géo qui ne sont pas
    # des entreprises (zones, places, rues, quartiers, lotissements...).
    "lieu_prefixe": re.compile(
        r"^("
        r"zone\s+(d\s?emplo|industriel|artisanal|d\s?activit|"
        r"commercial|franche|urbain|agricol|d\s?amenagement|"
        r"verte|natur|protege)|"
        r"z\s?a\s?c\b|z\s?i\b|"
        r"quartier\s|lotissement\s|cite\s|hameau\s|lieu[\s-]dit\s|"
        r"residence\s|bourg\s|"
        r"place\s+(de|du|des|d\s)|rue\s+(de|du|des|d\s)|"
        r"avenue\s+(de|du|des|d\s)|bd\s+(de|du|des|d\s)|"
        r"boulevard\s+(de|du|des|d\s)|allee\s+(de|du|des|d\s)|"
        r"rond[\s-]point\s|carrefour\s+(de|du|des|d\s)|"
        r"sentier\s|chemin\s+(de|du|des|d\s)|impasse\s+(de|du|des|d\s)|"
        r"pont\s+(de|du|des|d\s)|tunnel\s+(de|du|des|d\s)"
        r")",
        re.IGNORECASE,
    ),

    # Lieux/POI géographiques DANS le nom (peu importe la position)
    "lieu_dans_nom": re.compile(
        r"\b("
        r"parc\s+(d\s?activit|industriel|naturel|national|regional|"
        r"departemental|public|urbain|de\s+loisir|des\s+expositions?|"
        r"floral|botanique)|"
        r"aire\s+(de\s+jeux?|de\s+pique[\s-]nique|de\s+repos|de\s+service)|"
        r"plage(s)?\s|dunes?\s|cote\s+(d\s|sauvage)|"
        r"cimetiere(s)?|monument(s)?\s+aux|stele(s)?\s|memorial|"
        r"chateau\s+(de|du|des|d\s)|fort\s+(de|du|des|d\s)|"
        r"abbaye\s+(de|du|des|d\s)|"
        r"eglise\s+(de|du|des|d\s|saint|sainte|notre|st\s)|"
        r"cathedrale|chapelle\s+(de|du|des|d\s|saint)|"
        r"basilique|temple\s|synagogue|mosquee|"
        r"stade\s|gymnase|piscine\s+(municipale|de\s|du)|"
        r"bibliotheque|mediatheque|"
        r"office\s+national\s+des\s+forets?|onf\b|"
        r"foret\s+(domaniale|de|du|des)"
        r")\b",
        re.IGNORECASE,
    ),

    # Architecture, design, ingénierie, bureau d'études
    "architecture_design": re.compile(
        r"\b("
        r"architecte(s)?\s+d\s?interieur|"
        r"architecture\s+interieur|"
        r"decoration\s+interieur|decorateur(s)?|"
        r"studio\s+d\s?archi|atelier\s+d\s?archi|"
        r"bureau\s+d\s?etudes|ingenierie(\s+(du|en|des|services|bureau))?|"
        r"caue\b|conseil\s+d\s?architecture|odetec|"
        r"urbanisme\s+design|mc\s+architecture"
        r")\b",
        re.IGNORECASE,
    ),

    # Entreprises étrangères / langues étrangères
    "etranger_non_fr": re.compile(
        r"\b("
        r"limpiezas?|arquitect[oa]s?|estudio\s+de\s+arquitectura|"
        r"servicios?(\s+y\s+|\s+de\s+)|inmobiliaria|sociedad|"
        r"\bs\s?l\b|\bs\s?l\s+u\b|"  # Sociedad Limitada (SL, SLU)
        r"cleaning\s+service|building\s+(services?|maintenance)|"
        r"riells\s+vacacions|jaam\s+sociedad|blitzclean|ingelan|arkos|nexobau|"
        r"huw\s+webb"
        r")\b",
        re.IGNORECASE,
    ),

    # Traitement nuisibles, désinsectisation, démoussage
    "nuisibles_traitement": re.compile(
        r"\b("
        r"desinsectisation|deratisation|desinfection|"
        r"destruction\s+nuisibles?|anti[\s-]?nuisibles?|"
        r"demoussage|demoussement|"
        r"traitement\s+(de\s+)?(charpente|toiture|bois|termites?|merule|capricorne|humidite)|"
        r"stop[\s-]?taupes?|eco[\s-]?nuisibles?|fouine|mulot|"
        r"3d\s+(services?|hygiene)"
        r")\b",
        re.IGNORECASE,
    ),

    # Commerce alimentaire, artisans non-paysagistes, services de proximité
    "commerce_artisan": re.compile(
        r"\b("
        r"boulangerie(s)?|patisserie(s)?|"
        r"boucherie(s)?|charcuterie(s)?|poissonnerie(s)?|"
        r"epicerie(s)?|supermarche|hypermarche|superette|"
        r"pharmacie(s)?|parapharmacie|"
        r"restaurant(s)?|brasserie(s)?|bistrot|creperie|pizzeria|"
        r"salon\s+de\s+(coiffure|beaute|massage|the)|"
        r"coiffeur(se)?|coiffure|esthetique|institut\s+de\s+beaute|barbier|"
        r"fleuriste(s)?|"
        r"opticien(ne)?|optique\b|"
        r"librairie|tabac|presse\s+(et\s+|du)|"
        r"bijouterie|horlogerie|maroquinerie|"
        r"chausseur|chaussures\s+(et\s+|du)|cordonnerie"
        r")\b",
        re.IGNORECASE,
    ),

    # Automobile, garage, transport
    "automobile_transport": re.compile(
        r"\b("
        r"garage(s)?\s+(auto|de|du|des|d\s|peugeot|renault|citroen|ford|opel|fiat|volkswagen|toyota)|"
        r"concessionnaire(s)?|carrosserie|mecanique\s+auto|"
        r"auto[\s-]?ecole|moto[\s-]?ecole|"
        r"controle\s+technique|station[\s-]service|"
        r"pneus?\s+(et\s+|du|service)|depannage\s+auto|"
        r"transport(s)?\s+(de|du|des|public(s)?|routier(s)?|express|en\s+commun|de\s+marchandises?)|"
        r"taxi(s)?|vtc\b|deplacement(s)?"
        r")\b",
        re.IGNORECASE,
    ),

    # Santé / médical
    "sante_medical": re.compile(
        r"\b("
        r"cabinet\s+(medical|dentaire|d\s?infirmier|de\s+kinesi|d\s?osteo|de\s+sage|de\s+psycholog)|"
        r"medecin(s)?|dentiste(s)?|orthodontiste(s)?|"
        r"infirmier(e|s|es)?\s+(libe|a\s+domicile)|kinesi(therapeute)?(s)?|"
        r"osteopathe(s)?|psychologue(s)?|psychiatre(s)?|"
        r"laboratoire\s+(d\s?analyses?|medical|de\s+biologie)|"
        r"orthophoniste(s)?|orthoptiste(s)?|podologue(s)?|"
        r"veterinaire(s)?|clinique\s+veterinaire"
        r")\b",
        re.IGNORECASE,
    ),

    # Finance, assurance, droit, comptabilité
    "finance_droit": re.compile(
        r"\b("
        r"banque(s)?\s+(de|du|des|nationale|populaire|postale|cooperative)|"
        r"credit\s+(agricole|mutuel|du\s+nord|lyonnais|cooperatif|maritime)|"
        r"assurance(s)?\s+(maaf|matmut|axa|allianz|generali|de|du|des|mutuelle)|"
        r"notaire(s)?|huissier(s)?|avocat(s|e|es)?|"
        r"expert[\s-]comptable|cabinet\s+(comptable|d\s?avocats|d\s?expertise)|"
        r"agence\s+(immobiliere|de\s+voyage|bancaire)|"
        r"courtier(s)?|conseiller(s)?\s+financier(s)?"
        r")\b",
        re.IGNORECASE,
    ),

    # Divers très spécifiques (marques, produits)
    "divers_specifique": re.compile(
        r"\b("
        r"piscines?\s+de\s+france|"
        r"demenagement(s)?|location\s+(de\s+nacelles?|materiel|de\s+vehicules?)|"
        r"beton\s+(imprime|pret|arme|decoratif)|"
        r"valormat|dispano|natural\s+stone|lippi\b|artemat|"
        r"ozae|garden\s+park\s+concept|aquagaia|esso\s+turquoise|"
        r"centrale\s+(de\s+)?depannage|fls\b"
        r")\b",
        re.IGNORECASE,
    ),
}

# ── EXCLUSIONS RACHETABLES par un mot positif (BTP, nettoyage) ─────────────
# Une boîte de "menuiserie jardin" est probablement un vrai paysagiste, etc.
EXCLUSION_SAUVABLES_PATTERNS: dict[str, re.Pattern] = {

    "btp_construction": re.compile(
        r"\b("
        r"couvreur(s)?|toiture(s)?|"
        r"maconnerie|renovation\s+construction|"
        r"charpente(s)?|charpentier(s)?|"
        r"etancheite|ravalement|"
        r"facade(s)?|"
        r"platrerie|carrelage|carreleur|"
        r"peintre(\s+en\s+bat)?|peinture\s+(sarl|du|en|industriel)|"
        r"menuiserie(s)?|menuisier(s)?|fermeture(s)?|veranda(s)?|pergola(s)?|"
        r"electricite|electricien(ne)?|"
        r"plomberie|plombier(s)?|"
        r"chauffage(\s+sarl)?|isolation\s+(thermique|phonique|sarl)?"
        r")\b",
        re.IGNORECASE,
    ),

    "nettoyage_proprete": re.compile(
        r"\b("
        r"nettoyage\s+(professionnel|industriel|de\s+bureaux?|de\s+vitres?|tous?\s+services?)|"
        r"entreprise\s+de\s+nettoyage|"
        r"proprete(s)?\b|vitrerie|lavage(\s+|s\s)|"
        r"pressing|blanchisserie|laverie|"
        r"clinitex|isor|propnet|nikita\s+nettoyage|"
        r"foltier|karl\s+nettoyage|cnet\s+nettoyage|clean\s+now|master\s+net|"
        r"partenaire\s+service|edif[\s-]propre|activ[\s\']?clean|jon\s+net|"
        r"maison\s+net|nhps|spid\s+anjou"
        r")\b",
        re.IGNORECASE,
    ),
}

# Mots qui rachètent une exclusion sauvable
SAUVETAGE_RE = re.compile(
    r"\b(jardin(s|age)?|paysag(e|iste|isme)|elag(age|ueur)|espaces?\s+verts?)\b",
    re.IGNORECASE,
)

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


# Détection de scripts non-latins (cyrillique, arabe, hébreu, CJK, grec, etc.).
# Un nom dont la majorité des caractères-lettres est non-latine n'est pas un
# paysagiste français : à exclure d'office.
_NON_LATIN_RE = re.compile(
    r"[\u0370-\u03FF"   # Greek
    r"\u0400-\u04FF"    # Cyrillic
    r"\u0500-\u052F"    # Cyrillic supplement
    r"\u0590-\u05FF"    # Hebrew
    r"\u0600-\u06FF"    # Arabic
    r"\u0700-\u074F"    # Syriac
    r"\u0900-\u097F"    # Devanagari
    r"\u3040-\u30FF"    # Hiragana + Katakana
    r"\u3400-\u4DBF"    # CJK ext A
    r"\u4E00-\u9FFF"    # CJK
    r"\uAC00-\uD7AF"    # Hangul
    r"]"
)

def script_non_latin(name: str) -> bool:
    """True si la majorité des lettres du nom appartient à un script non-latin."""
    letters = [c for c in name if c.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for c in letters if _NON_LATIN_RE.match(c))
    return non_latin >= max(1, len(letters) // 2)

# ---------------------------------------------------------------------------
# NIVEAU 1 & 2 — Classification locale
# ---------------------------------------------------------------------------

def classifier_local(name: str) -> tuple[str | None, str]:
    """
    Retourne (decision, raison).
    decision : 'garder' | 'exclu' | None (ambigu → niveau 3)

    Pipeline :
      0a. script non-latin (cyrillique, arabe, CJK, …) → exclu
      0b. franchise connue (substring littéral) → exclu
      1.  mot paysagiste positif (regex GARDER_RE) → garder
      1b. mot positif conditionnel (abattage + contexte) → garder
      2.  pattern d'exclusion ferme (EXCLUSION_PATTERNS) → exclu
      3.  pattern d'exclusion sauvable (BTP/nettoyage) → exclu sauf si
          mot positif présent (sauvetage) → garder
      4.  ambigu → None
    """
    # ── 0a : script non-latin (testé sur le nom BRUT, avant normalisation) ──
    if script_non_latin(name):
        return "exclu", "script non-latin (pas un paysagiste FR)"

    n = normaliser(name)

    # ── 0b : franchises et marques (substring littéral) ──
    mot = contient_un(n, FRANCHISES_LITERAL)
    if mot:
        return "exclu", f"franchise: '{mot.strip()}'"

    # ── 1 : mots positifs (regex) ──
    m = GARDER_RE.search(n)
    if m:
        return "garder", f"mot positif: '{m.group(0)}'"

    # 1b. Mot positif conditionnel : "abattage" doit être combiné
    if ABATTAGE_RE.search(n) and ABATTAGE_CONTEXT_RE.search(n):
        return "garder", "mot positif conditionnel: 'abattage'"

    # ── 2 : exclusions fermes par catégorie ──
    for categorie, pattern in EXCLUSION_PATTERNS.items():
        m = pattern.search(n)
        if m:
            return "exclu", f"{categorie}: '{m.group(0).strip()}'"

    # ── 3 : exclusions rachetables par mot positif ──
    for categorie, pattern in EXCLUSION_SAUVABLES_PATTERNS.items():
        m = pattern.search(n)
        if m:
            sauvetage = SAUVETAGE_RE.search(n)
            if sauvetage:
                return "garder", (
                    f"sauvetage '{sauvetage.group(0)}' "
                    f"contre {categorie} '{m.group(0).strip()}'"
                )
            return "exclu", f"{categorie}: '{m.group(0).strip()}'"

    # ── 4 : ambigu (sera tranché par le code NAF en base) ──
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
