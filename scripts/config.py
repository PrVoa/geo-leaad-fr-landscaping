import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

# Charge .env depuis la racine du projet
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- Validation au démarrage -----------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise EnvironmentError(
        "DATABASE_URL manquant. Vérifiez votre fichier .env"
    )

# --- Paramètres configurables via .env -------------------------------------
HEADLESS         = os.getenv("HEADLESS", "true").lower() == "true"
MIN_DELAY        = float(os.getenv("MIN_DELAY", "10"))        # pause inter-ville (s)
MAX_DELAY        = float(os.getenv("MAX_DELAY", "15"))
MIN_DELAY_FICHE  = float(os.getenv("MIN_DELAY_FICHE", "2"))   # pause inter-fiche (s)
MAX_DELAY_FICHE  = float(os.getenv("MAX_DELAY_FICHE", "3"))
OBJECTIF_JOUR    = int(os.getenv("OBJECTIF_JOUR", "50"))
OBJECTIF_TOTAL   = int(os.getenv("OBJECTIF_TOTAL", "30000"))
CAPTCHA_WAIT     = int(os.getenv("CAPTCHA_WAIT", "900"))       # 15 min par défaut

# Modifiables par CLI
DRY_RUN = False

# --- Logging ---------------------------------------------------------------
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("scheduler")
    if logger.handlers:
        return logger  # déjà configuré
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    fh = RotatingFileHandler(
        LOGS_DIR / "scheduler.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)
    return logger


log = setup_logging()

# --- Données géographiques -------------------------------------------------
VILLES: dict[str, list[str]] = {
    "01": ["Bourg-en-Bresse", "Oyonnax", "Ambérieu-en-Bugey"],
    "06": ["Nice", "Cannes", "Antibes", "Menton", "Grasse"],
    "13": ["Marseille", "Aix-en-Provence", "Arles", "Martigues"],
    "21": ["Dijon", "Beaune", "Chenôve"],
    "25": ["Besançon", "Montbéliard", "Pontarlier"],
    "31": ["Toulouse", "Blagnac", "Colomiers", "Tournefeuille"],
    "33": ["Bordeaux", "Mérignac", "Pessac", "Talence"],
    "34": ["Montpellier", "Béziers", "Sète", "Agde"],
    "35": ["Rennes", "Saint-Malo", "Fougères", "Vitré"],
    "38": ["Grenoble", "Vienne", "Échirolles"],
    "44": ["Nantes", "Saint-Nazaire", "Saint-Herblain"],
    "45": ["Orléans", "Gien", "Montargis"],
    "49": ["Angers", "Cholet", "Saumur"],
    "51": ["Reims", "Châlons-en-Champagne", "Épernay"],
    "54": ["Nancy", "Vandœuvre-lès-Nancy", "Lunéville"],
    "57": ["Metz", "Thionville", "Forbach"],
    "59": ["Lille", "Roubaix", "Tourcoing", "Dunkerque", "Valenciennes"],
    "62": ["Calais", "Boulogne-sur-Mer", "Arras", "Lens"],
    "63": ["Clermont-Ferrand", "Riom", "Issoire"],
    "67": ["Strasbourg", "Haguenau", "Schiltigheim"],
    "69": ["Lyon", "Villeurbanne", "Vénissieux", "Saint-Priest", "Bron", "Caluire-et-Cuire"],
    "74": ["Annecy", "Thonon-les-Bains", "Annemasse"],
    "75": ["Paris 1er", "Paris 8ème", "Paris 15ème", "Paris 16ème"],
    "76": ["Rouen", "Le Havre", "Dieppe"],
    "77": ["Melun", "Meaux", "Fontainebleau"],
    "78": ["Versailles", "Saint-Germain-en-Laye", "Mantes-la-Jolie"],
    "80": ["Amiens", "Abbeville"],
    "83": ["Toulon", "Fréjus", "Hyères"],
    "84": ["Avignon", "Orange", "Carpentras"],
    "85": ["La Roche-sur-Yon", "Les Sables-d'Olonne"],
    "86": ["Poitiers", "Châtellerault"],
    "87": ["Limoges", "Saint-Junien"],
    "91": ["Évry", "Corbeil-Essonnes", "Massy"],
    "92": ["Nanterre", "Boulogne-Billancourt", "Colombes"],
    "93": ["Saint-Denis", "Montreuil", "Aubervilliers"],
    "94": ["Créteil", "Vincennes", "Vitry-sur-Seine"],
    "95": ["Cergy", "Argenteuil", "Sarcelles"],
}

MOTS_EXCLUS = [
    "velib", "belib", "parking", "station", "borne",
    "metro", "bus", "tram", "supermarche", "carrefour", "leclerc",
]
