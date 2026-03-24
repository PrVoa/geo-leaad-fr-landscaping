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
MIN_DELAY        = float(os.getenv("MIN_DELAY", "6"))         # pause inter-ville (s)
MAX_DELAY        = float(os.getenv("MAX_DELAY", "9"))
MIN_DELAY_FICHE  = float(os.getenv("MIN_DELAY_FICHE", "1"))   # pause inter-fiche (s)
MAX_DELAY_FICHE  = float(os.getenv("MAX_DELAY_FICHE", "1.5"))
OBJECTIF_JOUR    = int(os.getenv("OBJECTIF_JOUR", "50"))
OBJECTIF_TOTAL   = int(os.getenv("OBJECTIF_TOTAL", "50000"))
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
    # --- Métropole ---
    "01": ["Bourg-en-Bresse", "Oyonnax", "Ambérieu-en-Bugey"],
    "02": ["Laon", "Saint-Quentin", "Soissons", "Château-Thierry"],
    "03": ["Moulins", "Vichy", "Montluçon"],
    "04": ["Digne-les-Bains", "Manosque", "Sisteron"],
    "05": ["Gap", "Briançon"],
    "06": ["Nice", "Cannes", "Antibes", "Menton", "Grasse"],
    "07": ["Privas", "Annonay", "Aubenas"],
    "08": ["Charleville-Mézières", "Sedan", "Rethel"],
    "09": ["Foix", "Pamiers", "Saint-Girons"],
    "10": ["Troyes", "Romilly-sur-Seine", "Bar-sur-Aube"],
    "11": ["Carcassonne", "Narbonne", "Castelnaudary"],
    "12": ["Rodez", "Millau", "Villefranche-de-Rouergue"],
    "13": ["Marseille", "Aix-en-Provence", "Arles", "Martigues"],
    "14": ["Caen", "Hérouville-Saint-Clair", "Lisieux", "Bayeux"],
    "15": ["Aurillac", "Saint-Flour"],
    "16": ["Angoulême", "Cognac", "Soyaux"],
    "17": ["La Rochelle", "Rochefort", "Saintes", "Royan"],
    "18": ["Bourges", "Vierzon", "Saint-Amand-Montrond"],
    "19": ["Tulle", "Brive-la-Gaillarde", "Ussel"],
    "2A": ["Ajaccio", "Porto-Vecchio", "Sartène"],
    "2B": ["Bastia", "Corte", "Calvi"],
    "21": ["Dijon", "Beaune", "Chenôve"],
    "22": ["Saint-Brieuc", "Lannion", "Dinan", "Loudéac"],
    "23": ["Guéret", "La Souterraine"],
    "24": ["Périgueux", "Bergerac", "Sarlat-la-Canéda"],
    "25": ["Besançon", "Montbéliard", "Pontarlier"],
    "26": ["Valence", "Romans-sur-Isère", "Montélimar"],
    "27": ["Évreux", "Vernon", "Bernay", "Louviers"],
    "28": ["Chartres", "Dreux", "Châteaudun"],
    "29": ["Brest", "Quimper", "Morlaix", "Concarneau"],
    "30": ["Nîmes", "Alès", "Bagnols-sur-Cèze"],
    "31": ["Toulouse", "Blagnac", "Colomiers", "Tournefeuille"],
    "32": ["Auch", "Condom"],
    "33": ["Bordeaux", "Mérignac", "Pessac", "Talence"],
    "34": ["Montpellier", "Béziers", "Sète", "Agde"],
    "35": ["Rennes", "Saint-Malo", "Fougères", "Vitré"],
    "36": ["Châteauroux", "Issoudun"],
    "37": ["Tours", "Joué-lès-Tours", "Saint-Pierre-des-Corps", "Amboise"],
    "38": ["Grenoble", "Vienne", "Échirolles"],
    "39": ["Lons-le-Saunier", "Dole", "Saint-Claude"],
    "40": ["Mont-de-Marsan", "Dax", "Biscarrosse"],
    "41": ["Blois", "Vendôme", "Romorantin-Lanthenay"],
    "42": ["Saint-Étienne", "Roanne", "Firminy"],
    "43": ["Le Puy-en-Velay", "Brioude", "Yssingeaux"],
    "44": ["Nantes", "Saint-Nazaire", "Saint-Herblain"],
    "45": ["Orléans", "Gien", "Montargis"],
    "46": ["Cahors", "Figeac"],
    "47": ["Agen", "Marmande", "Villeneuve-sur-Lot"],
    "48": ["Mende", "Marvejols"],
    "49": ["Angers", "Cholet", "Saumur"],
    "50": ["Cherbourg-en-Cotentin", "Saint-Lô", "Avranches", "Granville"],
    "51": ["Reims", "Châlons-en-Champagne", "Épernay"],
    "52": ["Chaumont", "Saint-Dizier", "Langres"],
    "53": ["Laval", "Mayenne", "Château-Gontier"],
    "54": ["Nancy", "Vandœuvre-lès-Nancy", "Lunéville"],
    "55": ["Bar-le-Duc", "Verdun", "Commercy"],
    "56": ["Vannes", "Lorient", "Pontivy", "Auray"],
    "57": ["Metz", "Thionville", "Forbach"],
    "58": ["Nevers", "Cosne-Cours-sur-Loire"],
    "59": ["Lille", "Roubaix", "Tourcoing", "Dunkerque", "Valenciennes"],
    "60": ["Beauvais", "Compiègne", "Creil", "Senlis"],
    "61": ["Alençon", "Flers", "Argentan"],
    "62": ["Calais", "Boulogne-sur-Mer", "Arras", "Lens"],
    "63": ["Clermont-Ferrand", "Riom", "Issoire"],
    "64": ["Pau", "Bayonne", "Biarritz"],
    "65": ["Tarbes", "Lourdes"],
    "66": ["Perpignan", "Canet-en-Roussillon"],
    "67": ["Strasbourg", "Haguenau", "Schiltigheim"],
    "68": ["Mulhouse", "Colmar", "Illzach"],
    "69": ["Lyon", "Villeurbanne", "Vénissieux", "Saint-Priest", "Bron", "Caluire-et-Cuire"],
    "70": ["Vesoul", "Lure", "Gray"],
    "71": ["Mâcon", "Chalon-sur-Saône", "Autun"],
    "72": ["Le Mans", "La Flèche", "Allonnes"],
    "73": ["Chambéry", "Albertville", "Aix-les-Bains"],
    "74": ["Annecy", "Thonon-les-Bains", "Annemasse"],
    "75": ["Paris 1er", "Paris 8ème", "Paris 15ème", "Paris 16ème"],
    "76": ["Rouen", "Le Havre", "Dieppe"],
    "77": ["Melun", "Meaux", "Fontainebleau"],
    "78": ["Versailles", "Saint-Germain-en-Laye", "Mantes-la-Jolie"],
    "79": ["Niort", "Bressuire", "Parthenay"],
    "80": ["Amiens", "Abbeville"],
    "81": ["Albi", "Castres", "Gaillac"],
    "82": ["Montauban", "Moissac", "Castelsarrasin"],
    "83": ["Toulon", "Fréjus", "Hyères"],
    "84": ["Avignon", "Orange", "Carpentras"],
    "85": ["La Roche-sur-Yon", "Les Sables-d'Olonne"],
    "86": ["Poitiers", "Châtellerault"],
    "87": ["Limoges", "Saint-Junien"],
    "88": ["Épinal", "Saint-Dié-des-Vosges", "Remiremont"],
    "89": ["Auxerre", "Sens", "Avallon"],
    "90": ["Belfort"],
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

# Catégories Google Maps à exclure (affiché sous le nom de l'établissement)
CATEGORIES_EXCLUES: list[str] = [
    "association", "ong", "organisation à but non lucratif",
    "administration", "mairie", "collectivité", "syndicat",
    "école", "université", "lycée", "collège", "formation",
    "architecte", "bureau d'études", "cabinet",
    "photographe", "château", "hôtel", "camping", "gîte",
    "pépinière", "jardinerie", "animalerie", "fleuriste",
    "grande surface", "magasin",
]

# Termes de recherche Google Maps (un seul pour rester précis)
MOTS_CLES_RECHERCHE: list[str] = [
    "paysagiste",
]
