"""
Module de logging centralisé.

Tous les scripts importent depuis ce module :
    from logger import get_logger
    log = get_logger("mon_script")

Tous les logs vont dans logs/app.log (rotation 10 MB × 5 fichiers).
Format : timestamp | niveau | script | message
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "app.log"

_FMT = logging.Formatter(
    "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Logger racine commun à tous les scripts du projet
_root = logging.getLogger("geoleaad")
if not _root.handlers:
    _root.setLevel(logging.DEBUG)

    # Handler console (INFO+)
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(_FMT)

    # Handler fichier rotatif (DEBUG+, 10 MB × 5)
    _fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(_FMT)

    _root.addHandler(_ch)
    _root.addHandler(_fh)
    _root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Retourne un logger enfant de 'geoleaad', qui écrit dans logs/app.log."""
    return logging.getLogger(f"geoleaad.{name}")
