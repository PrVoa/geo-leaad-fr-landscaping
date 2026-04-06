"""
API FastAPI — contrôle des agents de scraping/nettoyage/enrichissement.

Variables d'environnement requises :
  DATABASE_URL  — URL PostgreSQL (même que scripts/.env)
  API_KEY       — clé d'authentification pour le header X-API-Key

Lancement :
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import calendar
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Security, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config (importée depuis scripts/config.py)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from config import DATABASE_URL, API_KEY, DB_URL as _asyncpg_url, ANTHROPIC_API_KEY  # noqa: E402

SCRIPTS_DIR = ROOT / "scripts"
LOGS_DIR = ROOT / "logs"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Pool unique — créé une seule fois au démarrage, max 3 connexions
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None

# Singleton Anthropic — créé une seule fois, réutilise les connexions HTTP keep-alive
_anthropic_client: Optional[object] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    if _asyncpg_url:
        # max_size=5 : permet 3 requêtes /stats + 2 requêtes /leads en parallèle
        _pool = await asyncpg.create_pool(
            _asyncpg_url,
            min_size=1,
            max_size=5,
            command_timeout=10,
        )
    yield
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="GeoLeaad API", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)


async def verify_api_key(key: str = Security(api_key_header)) -> str:
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Clé API invalide")
    return key


# ---------------------------------------------------------------------------
# État des agents (en mémoire — suffit pour un seul processus uvicorn)
# ---------------------------------------------------------------------------

class AgentState:
    def __init__(self, name: str):
        self.name = name
        self.process: Optional[subprocess.Popen] = None
        self.started_at: Optional[datetime] = None
        self.dept: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> bool:
        if not self.running:
            return False
        try:
            self.process.terminate()
        except ProcessLookupError:
            pass
        return True

    def start(self, cmd: list[str], dept: Optional[str] = None) -> None:
        if self.running:
            raise HTTPException(status_code=409, detail=f"Agent {self.name} déjà en cours")
        log_file = LOGS_DIR / f"{self.name}.log"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        # Fermeture explicite du handle : subprocess hérite via os.dup(), notre copie est inutile
        with open(log_file, "a") as log_fh:
            self.process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
            )
        self.started_at = datetime.utcnow()
        self.dept = dept


scraper_state = AgentState("scheduler")
clean_state = AgentState("clean_leads")
enrich_state = AgentState("enrich")


# ---------------------------------------------------------------------------
# Helpers DB — réutilise le pool unique, libère la connexion après chaque usage
# ---------------------------------------------------------------------------

async def _pool_or_error() -> asyncpg.Pool:
    if not _pool:
        raise HTTPException(status_code=500, detail="Pool DB non initialisé")
    return _pool


async def query_one(sql: str, *args):
    pool = await _pool_or_error()
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def query_all(sql: str, *args):
    pool = await _pool_or_error()
    async with pool.acquire() as conn:
        return await conn.fetch(sql, *args)


# ---------------------------------------------------------------------------
# Routeur IA — haiku-4-5 (< 30 mots) ou sonnet-4-5 (≥ 30 mots), jamais Opus
# ---------------------------------------------------------------------------

MODEL_HAIKU  = "claude-haiku-4-5-20251001"
MODEL_SONNET = "claude-sonnet-4-5-20251015"

# Tarifs en $ par million de tokens (input / output)
_PRIX: dict[str, tuple[float, float]] = {
    MODEL_HAIKU:  (0.80, 4.00),
    MODEL_SONNET: (3.00, 15.00),
}


def _choisir_modele(question: str) -> str:
    """Haiku si ≤ 30 mots, Sonnet sinon."""
    return MODEL_HAIKU if len(question.split()) <= 30 else MODEL_SONNET


def _type_question(model: str) -> str:
    return "question_libre" if model == MODEL_HAIKU else "resume_analyse"


@dataclass
class _BucketTokens:
    input: int = 0
    output: int = 0

    def ajouter(self, inp: int, out: int) -> None:
        self.input += inp
        self.output += out

    def cout_usd(self, model: str) -> float:
        p_in, p_out = _PRIX[model]
        return (self.input * p_in + self.output * p_out) / 1_000_000


@dataclass
class _UsageIA:
    date_courante: date = field(default_factory=date.today)
    question_libre: _BucketTokens = field(default_factory=_BucketTokens)
    resume_analyse: _BucketTokens = field(default_factory=_BucketTokens)

    def _verifier_reset(self) -> None:
        today = date.today()
        if today != self.date_courante:
            self.date_courante = today
            self.question_libre = _BucketTokens()
            self.resume_analyse = _BucketTokens()

    def enregistrer(self, type_q: str, model: str, inp: int, out: int) -> None:
        self._verifier_reset()
        bucket: _BucketTokens = getattr(self, type_q)
        bucket.ajouter(inp, out)

    def snapshot(self) -> dict:
        self._verifier_reset()
        today = date.today()
        day_of_month = today.day
        days_in_month = calendar.monthrange(today.year, today.month)[1]

        cout_ql = self.question_libre.cout_usd(MODEL_HAIKU)
        cout_ra = self.resume_analyse.cout_usd(MODEL_SONNET)
        cout_jour = cout_ql + cout_ra
        projection = round(cout_jour / day_of_month * days_in_month, 4) if day_of_month > 0 else 0.0

        return {
            "date": today.isoformat(),
            "question_libre": {
                "tokens_input": self.question_libre.input,
                "tokens_output": self.question_libre.output,
                "modele": MODEL_HAIKU,
                "cout_usd": round(cout_ql, 6),
            },
            "resume_analyse": {
                "tokens_input": self.resume_analyse.input,
                "tokens_output": self.resume_analyse.output,
                "modele": MODEL_SONNET,
                "cout_usd": round(cout_ra, 6),
            },
            "cout_total_jour_usd": round(cout_jour, 6),
            "projection_fin_mois_usd": projection,
        }


_usage_ia = _UsageIA()


class QuestionPayload(BaseModel):
    question: str
    system: Optional[str] = None


@app.post("/ask", dependencies=[Security(verify_api_key)])
async def ask(payload: QuestionPayload):
    """Pose une question à Claude. Modèle sélectionné automatiquement selon la longueur."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY non configurée")

    try:
        import anthropic as _anthropic
    except ImportError:
        raise HTTPException(status_code=503, detail="Package anthropic non installé (pip install anthropic)")

    model = _choisir_modele(payload.question)
    type_q = _type_question(model)

    # Singleton : réutilise la session HTTPX et les connexions keep-alive entre les requêtes
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = _anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    client = _anthropic_client

    kwargs: dict = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": payload.question}]}
    if payload.system:
        kwargs["system"] = payload.system

    try:
        response = client.messages.create(**kwargs)
    except _anthropic.APIError as exc:
        raise HTTPException(status_code=502, detail=f"Erreur API Claude : {exc}")

    inp = response.usage.input_tokens
    out = response.usage.output_tokens
    _usage_ia.enregistrer(type_q, model, inp, out)

    return {
        "reponse": response.content[0].text,
        "modele": model,
        "type": type_q,
        "tokens": {"input": inp, "output": out},
    }


# ---------------------------------------------------------------------------
# Cache mémoire 30 secondes — évite les requêtes répétées de Lovable
# ---------------------------------------------------------------------------

class SimpleCache:
    """Cache LRU-TTL : éviction des entrées expirées + limite de taille pour prévenir la fuite mémoire."""

    def __init__(self, ttl: float = 30.0, maxsize: int = 512):
        self._ttl = ttl
        self._maxsize = maxsize
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry[0] >= self._ttl:
            # Purge à la lecture : évite de retourner du périmé
            del self._store[key]
            return None
        return entry[1]

    def set(self, key: str, value):
        now = time.monotonic()
        # Purge des entrées expirées avant d'ajouter (évite la croissance illimitée)
        if len(self._store) >= self._maxsize:
            expired = [k for k, (t, _) in self._store.items() if now - t >= self._ttl]
            for k in expired:
                del self._store[k]
            # Si toujours plein après purge TTL → vider la plus ancienne entrée
            if len(self._store) >= self._maxsize:
                oldest = min(self._store, key=lambda k: self._store[k][0])
                del self._store[oldest]
        self._store[key] = (now, value)


_cache = SimpleCache(ttl=30.0, maxsize=512)


# ---------------------------------------------------------------------------
# Endpoints — Scraper
# ---------------------------------------------------------------------------

@app.post("/agents/scraper/start", dependencies=[Security(verify_api_key)])
async def scraper_start(dept: Optional[str] = Query(None, description="Code département ex: 69")):
    cmd = [PYTHON, str(SCRIPTS_DIR / "scheduler.py"), "--headless", "true"]
    if dept:
        dept = dept.zfill(2)
        cmd += ["--dept", dept]
    scraper_state.start(cmd, dept=dept)
    return {
        "status": "started",
        "dept": dept or "tous",
        "pid": scraper_state.process.pid,
    }


@app.post("/agents/scraper/stop", dependencies=[Security(verify_api_key)])
async def scraper_stop():
    if not scraper_state.stop():
        raise HTTPException(status_code=409, detail="Le scraper n'est pas en cours")
    return {"status": "stopping", "pid": scraper_state.process.pid}


@app.get("/agents/scraper/status", dependencies=[Security(verify_api_key)])
async def scraper_status():
    today = date.today()

    leads_today = 0
    try:
        row = await query_one(
            "SELECT COUNT(*) AS n FROM landscapers WHERE scraped_at::date = $1",
            today,
        )
        leads_today = row["n"] if row else 0
    except Exception:
        leads_today = None

    vitesse = None
    if scraper_state.running and scraper_state.started_at and leads_today:
        elapsed_h = (datetime.utcnow() - scraper_state.started_at).total_seconds() / 3600
        vitesse = round(leads_today / elapsed_h) if elapsed_h > 0.01 else 0

    return {
        "running": scraper_state.running,
        "dept": scraper_state.dept or "tous",
        "started_at": scraper_state.started_at.isoformat() if scraper_state.started_at else None,
        "pid": scraper_state.process.pid if scraper_state.process else None,
        "leads_today": leads_today,
        "vitesse_par_heure": vitesse,
    }


# ---------------------------------------------------------------------------
# Endpoints — Clean
# ---------------------------------------------------------------------------

@app.post("/agents/clean/start", dependencies=[Security(verify_api_key)])
async def clean_start():
    cmd = [PYTHON, str(SCRIPTS_DIR / "clean_leads.py")]
    clean_state.start(cmd)
    return {"status": "started", "pid": clean_state.process.pid}


@app.post("/agents/clean/stop", dependencies=[Security(verify_api_key)])
async def clean_stop():
    if not clean_state.stop():
        raise HTTPException(status_code=409, detail="L'agent clean n'est pas en cours")
    return {"status": "stopping", "pid": clean_state.process.pid}


@app.get("/agents/clean/status", dependencies=[Security(verify_api_key)])
async def clean_status():
    return {
        "running": clean_state.running,
        "started_at": clean_state.started_at.isoformat() if clean_state.started_at else None,
        "pid": clean_state.process.pid if clean_state.process else None,
    }


# ---------------------------------------------------------------------------
# Endpoints — Enrich
# ---------------------------------------------------------------------------

@app.post("/agents/enrich/start", dependencies=[Security(verify_api_key)])
async def enrich_start():
    cmd = [PYTHON, str(SCRIPTS_DIR / "enrich.py")]
    enrich_state.start(cmd)
    return {"status": "started", "pid": enrich_state.process.pid}


@app.post("/agents/enrich/stop", dependencies=[Security(verify_api_key)])
async def enrich_stop():
    if not enrich_state.stop():
        raise HTTPException(status_code=409, detail="L'agent enrich n'est pas en cours")
    return {"status": "stopping", "pid": enrich_state.process.pid}


@app.get("/agents/enrich/status", dependencies=[Security(verify_api_key)])
async def enrich_status():
    return {
        "running": enrich_state.running,
        "started_at": enrich_state.started_at.isoformat() if enrich_state.started_at else None,
        "pid": enrich_state.process.pid if enrich_state.process else None,
    }


# ---------------------------------------------------------------------------
# Endpoint — Leads (avec cache 30s)
# ---------------------------------------------------------------------------

@app.get("/leads", dependencies=[Security(verify_api_key)])
async def get_leads(
    limit: int = Query(50, ge=1, le=500, description="Nombre de résultats"),
    offset: int = Query(0, ge=0, description="Décalage pour la pagination"),
    dept: Optional[str] = Query(None, description="Filtrer par département ex: 69"),
    statut: Optional[str] = Query(None, description="Filtrer par statut ex: nouveau, contacté"),
    search: Optional[str] = Query(None, description="Recherche dans nom, adresse, téléphone, email, notes, assigné"),
):
    cache_key = f"leads:{limit}:{offset}:{dept}:{statut}:{search}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    conditions = []
    args = []

    if dept:
        args.append(dept.zfill(2))
        conditions.append(f"dept = ${len(args)}")

    if statut:
        args.append(statut)
        conditions.append(f"statut = ${len(args)}")

    if search:
        args.append(f"%{search}%")
        conditions.append(
            f"(name ILIKE ${len(args)}"
            f" OR address ILIKE ${len(args)}"
            f" OR phone ILIKE ${len(args)}"
            f" OR email ILIKE ${len(args)}"
            f" OR notes ILIKE ${len(args)}"
            f" OR assigne_a ILIKE ${len(args)})"
        )

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    args_count = args.copy()
    args.append(limit)
    args.append(offset)

    sql = f"""
        SELECT
            place_id, name, phone, address, website, email,
            rating, review_count, maps_url, scraped_at,
            categorie, dept, statut
        FROM landscapers
        {where}
        ORDER BY scraped_at DESC
        LIMIT ${len(args) - 1} OFFSET ${len(args)}
    """

    sql_count = f"SELECT COUNT(*) AS n FROM landscapers {where}"

    try:
        pool = await _pool_or_error()
        # Deux connexions parallèles depuis le pool — réduit la latence perçue de 2 round-trips à 1
        rows, count_row = await asyncio.gather(
            pool.fetch(sql, *args),
            pool.fetchrow(sql_count, *args_count),
        )
        total = count_row["n"] if count_row else 0
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur DB : {exc}")

    result = {
        "total": total,
        "limit": limit,
        "offset": offset,
        "data": [dict(r) for r in rows],
    }
    _cache.set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Endpoint — Stats globales (avec cache 30s)
# ---------------------------------------------------------------------------

@app.get("/stats", dependencies=[Security(verify_api_key)])
async def stats():
    cached = _cache.get("stats")
    if cached is not None:
        return cached

    today = date.today()

    try:
        pool = await _pool_or_error()
        # 3 connexions parallèles depuis le pool — 3 round-trips → 1 round-trip perçu
        row_total, row_today, rows_statut = await asyncio.gather(
            pool.fetchrow("SELECT COUNT(*) AS n FROM landscapers"),
            pool.fetchrow(
                "SELECT COUNT(*) AS n FROM landscapers WHERE scraped_at::date = $1", today
            ),
            pool.fetch(
                "SELECT COALESCE(statut, 'nouveau') AS statut, COUNT(*) AS n"
                " FROM landscapers GROUP BY statut ORDER BY n DESC"
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erreur DB : {exc}")

    result = {
        "total_leads": row_total["n"] if row_total else 0,
        "leads_aujourd_hui": row_today["n"] if row_today else 0,
        "repartition_statut": {r["statut"]: r["n"] for r in rows_statut},
        "date": today.isoformat(),
        "ia": _usage_ia.snapshot(),
    }
    _cache.set("stats", result)
    return result


# ---------------------------------------------------------------------------
# Endpoint — Logs
# ---------------------------------------------------------------------------

@app.get("/logs", dependencies=[Security(verify_api_key)])
async def logs(
    agent: str = Query("", description="Filtrer par script (scheduler, clean_leads, enrich, …). Vide = tous."),
    lines: int = Query(100, ge=1, le=1000),
):
    # Tous les logs sont dans app.log depuis l'étape-3
    log_file = LOGS_DIR / "app.log"
    if not log_file.exists():
        return {"agent": agent or "all", "lines": [], "message": "Aucun log trouvé"}

    # I/O fichier exécuté dans un thread pool pour ne pas bloquer l'event loop asyncio
    def _read_log() -> list[str]:
        buf: deque[str] = deque(maxlen=lines)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not agent or f"geoleaad.{agent}" in line:
                    buf.append(line.rstrip())
        return list(buf)

    result = await asyncio.get_event_loop().run_in_executor(None, _read_log)
    return {"agent": agent or "all", "lines": result}
