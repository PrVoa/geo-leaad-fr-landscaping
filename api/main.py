"""
API FastAPI — contrôle des agents de scraping/nettoyage/enrichissement.

Variables d'environnement requises :
  DATABASE_URL  — URL PostgreSQL (même que scripts/.env)
  API_KEY       — clé d'authentification pour le header X-API-Key

Lancement :
  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import os
import subprocess
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Security, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATABASE_URL: str = os.getenv("DATABASE_URL", "")
API_KEY: str = os.getenv("API_KEY", "changeme")

# asyncpg attend postgresql:// et non postgresql+asyncpg://
_asyncpg_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

SCRIPTS_DIR = ROOT / "scripts"
LOGS_DIR = ROOT / "logs"
PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Pool unique — créé une seule fois au démarrage, max 3 connexions
# ---------------------------------------------------------------------------

_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    if _asyncpg_url:
        _pool = await asyncpg.create_pool(
            _asyncpg_url,
            min_size=1,
            max_size=3,
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
        self.process = subprocess.Popen(
            cmd,
            stdout=open(log_file, "a"),
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
# Cache mémoire 30 secondes — évite les requêtes répétées de Lovable
# ---------------------------------------------------------------------------

class SimpleCache:
    def __init__(self, ttl: float = 30.0):
        self._ttl = ttl
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        entry = self._store.get(key)
        if entry and (time.monotonic() - entry[0]) < self._ttl:
            return entry[1]
        return None

    def set(self, key: str, value):
        self._store[key] = (time.monotonic(), value)


_cache = SimpleCache(ttl=30.0)


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
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
            count_row = await conn.fetchrow(sql_count, *args_count)
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
        async with pool.acquire() as conn:
            row_total = await conn.fetchrow("SELECT COUNT(*) AS n FROM landscapers")
            row_today = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM landscapers WHERE scraped_at::date = $1",
                today,
            )
            rows_statut = await conn.fetch(
                """
                SELECT
                    COALESCE(statut, 'nouveau') AS statut,
                    COUNT(*) AS n
                FROM landscapers
                GROUP BY statut
                ORDER BY n DESC
                """
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
    }
    _cache.set("stats", result)
    return result


# ---------------------------------------------------------------------------
# Endpoint — Logs
# ---------------------------------------------------------------------------

@app.get("/logs", dependencies=[Security(verify_api_key)])
async def logs(
    agent: str = Query("scheduler", description="scheduler | clean_leads | enrich_leads"),
    lines: int = Query(100, ge=1, le=1000),
):
    log_file = LOGS_DIR / f"{agent}.log"
    if not log_file.exists():
        return {"agent": agent, "lines": [], "message": "Aucun log trouvé"}

    buf: deque[str] = deque(maxlen=lines)
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            buf.append(line.rstrip())

    return {"agent": agent, "lines": list(buf)}
