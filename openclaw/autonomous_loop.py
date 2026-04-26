"""
autonomous_loop.py — Composants pipeline pour Azor II.
Wrappers minimaux pour lancer scraper / enrich / clean depuis les commandes
manuelles (/start_scraper, /start_enrich). Le mode autonome a été retiré ;
plus rien ici n'utilise Claude.
"""

import subprocess

from core.shared import (
    now_utc as _now_utc,
    load_json as _load_json,
    save_json as _save_json,
)
from config import (
    ALERT_FILE,
    PIPELINE_VENV_PYTHON, SCRAPER_SCRIPT, ENRICH_SCRIPT, CLEAN_SCRIPT,
    SCRAPER_LOG_FILE, CLEAN_LOG_FILE,
    ENRICH_SYSTEMD_UNIT,
)


SCRAPER_CMD = [PIPELINE_VENV_PYTHON, SCRAPER_SCRIPT]
ENRICH_CMD  = [PIPELINE_VENV_PYTHON, ENRICH_SCRIPT]
CLEAN_CMD   = [PIPELINE_VENV_PYTHON, CLEAN_SCRIPT]


# ─── LANCEMENT PROCESSUS ──────────────────────────────────────────────────────

class SystemdProc:
    """Wrapper qui mime l'API Popen (.poll/.pid/.returncode/.terminate)
    mais pilote un unit systemd."""

    def __init__(self, unit: str, start: bool = True):
        self.unit = unit
        self.pid  = f"systemd:{unit}"
        if start:
            self._start()

    def _start(self):
        r = subprocess.run(
            ["systemctl", "start", self.unit],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"systemctl start {self.unit} a échoué "
                f"(code {r.returncode}) : {r.stderr.decode(errors='replace').strip()}"
            )

    def poll(self) -> int | None:
        """None si le unit est encore `active`, sinon le dernier ExecMainStatus."""
        r = subprocess.run(
            ["systemctl", "is-active", self.unit],
            capture_output=True, timeout=10,
        )
        state = r.stdout.decode().strip()
        if state == "active" or state == "activating":
            return None
        r = subprocess.run(
            ["systemctl", "show", self.unit, "-p", "ExecMainStatus", "--value"],
            capture_output=True, timeout=10,
        )
        try:
            return int(r.stdout.decode().strip())
        except (ValueError, TypeError):
            return 0

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def terminate(self):
        subprocess.run(
            ["systemctl", "stop", self.unit],
            capture_output=True, timeout=30,
        )


def launch_stage(stage: str, cmd: list[str]):
    """Lance un stage du pipeline. Retourne un objet type-Popen.
    - enrich → systemctl start (unit indépendant)
    - scraper/clean → subprocess.Popen avec stdout/stderr vers fichier
    """
    if stage == "enrich":
        return SystemdProc(ENRICH_SYSTEMD_UNIT)

    log_path = SCRAPER_LOG_FILE if stage == "scraper" else CLEAN_LOG_FILE
    with open(log_path, "a", buffering=1) as f:
        return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)


def _record_stage_started(stage: str):
    """Marque qu'un stage a démarré (réinitialise la fin précédente)."""
    alert = _load_json(ALERT_FILE, {})
    alert[f"last_{stage}_started"]   = _now_utc().isoformat()
    alert.pop(f"last_{stage}_finished",  None)
    alert.pop(f"last_{stage}_exit_code", None)
    _save_json(ALERT_FILE, alert)
