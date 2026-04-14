"""
Infra réutilisable pour bots Telegram conversationnels assistés par Claude.
Ne contient AUCUNE logique métier (pas de KB, pas de prompts, pas de check-in).
Forkable tel quel pour démarrer un nouveau client.
"""
import os, json, datetime, re
from pathlib import Path
from dotenv import load_dotenv
import anthropic
from telegram import Bot, Update

load_dotenv("/opt/openclaw/.env")


# ─── ENV CORE (le minimum pour qu'un bot Telegram + Claude fonctionne) ───────
BOT_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS          = [c for c in [
    os.getenv("TELEGRAM_CHAT_ID_1"),
    os.getenv("TELEGRAM_CHAT_ID_2"),
] if c]
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ─── SINGLETONS ──────────────────────────────────────────────────────────────
_bot_instance: Bot | None = None
def bot() -> Bot:
    """Singleton Bot — une seule session HTTP/SSL réutilisée."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = Bot(token=BOT_TOKEN)
    return _bot_instance

_anthropic_instance: anthropic.Anthropic | None = None
def anthropic_client() -> anthropic.Anthropic:
    """Singleton client Anthropic."""
    global _anthropic_instance
    if _anthropic_instance is None:
        _anthropic_instance = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_instance


# ─── TIME ────────────────────────────────────────────────────────────────────
def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def now_paris() -> datetime.datetime:
    return now_utc().astimezone(datetime.timezone(datetime.timedelta(hours=2)))


# ─── JSON I/O (défensif) ─────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─── MARKDOWN TELEGRAM ───────────────────────────────────────────────────────
def strip_markdown(s) -> str:
    """Supprime les caractères Markdown (utile pour fallback plain)."""
    return re.sub(r'[*_`\[\]\\]', '', str(s))

def escape_markdown(s) -> str:
    """Échappe les caractères Markdown (préserve le texte tel quel)."""
    out = str(s)
    for ch in ["_", "*", "`", "["]:
        out = out.replace(ch, f"\\{ch}")
    return out


# ─── DÉCOUPAGE TELEGRAM ──────────────────────────────────────────────────────
TG_LIMIT_HARD = 4096
TG_SAFE_LIMIT = 3900

def _split_text(text: str, limit: int = TG_SAFE_LIMIT) -> list[str]:
    """Découpe robuste sur \\n\\n > \\n > espace > brut. Garantit < limit chars."""
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            parts.append(remaining)
            break
        window = remaining[:limit]
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                parts.append(remaining[:cut])
                remaining = remaining[cut:].lstrip("\n").lstrip()
                break
        else:
            parts.append(remaining[:limit])
            remaining = remaining[limit:]
    safe_parts: list[str] = []
    for p in parts:
        if len(p) <= limit:
            safe_parts.append(p)
        else:
            for i in range(0, len(p), limit):
                safe_parts.append(p[i:i + limit])
    return safe_parts


def _number_parts(parts: list[str]) -> list[str]:
    n = len(parts)
    if n <= 1:
        return parts
    return [f"[{i + 1}/{n}] {p}" for i, p in enumerate(parts)]


async def _send_one_part(send_callable, part: str, markdown: bool) -> bool:
    """Envoie une partie avec fallback Markdown→plain. Avale les exceptions."""
    if markdown:
        try:
            await send_callable(part, parse_mode="Markdown")
            return True
        except Exception as e:
            print(f"[telegram] Markdown send échoué ({type(e).__name__}: {str(e)[:120]}) — retry plain")
    plain = strip_markdown(part) if markdown else part
    try:
        await send_callable(plain)
        return True
    except Exception as e:
        print(f"[telegram] Plain send échoué aussi ({type(e).__name__}: {str(e)[:120]}) — partie perdue")
        return False


# ─── ENVOI ───────────────────────────────────────────────────────────────────
async def send(chat_id: str, text: str, markdown: bool = True):
    """Envoie un message Telegram à un chat_id, avec découpage et fallback."""
    b = bot()
    parts = _number_parts(_split_text(text))
    sent = 0
    for part in parts:
        async def _do(p, **kw):
            await b.send_message(chat_id=chat_id, text=p, **kw)
        if await _send_one_part(_do, part, markdown=markdown):
            sent += 1
    if sent < len(parts):
        print(f"[telegram] send({chat_id}) : {sent}/{len(parts)} parties envoyées")


async def broadcast(text: str, markdown: bool = True):
    """Envoie à tous les CHAT_IDS configurés."""
    for cid in CHAT_IDS:
        await send(cid, text, markdown=markdown)


async def safe_reply(update: Update, text: str, markdown: bool = True):
    """Répond à un update Telegram avec découpage et fallback Markdown."""
    # effective_message = update.message pour une commande,
    # update.callback_query.message quand l'update vient d'un bouton inline.
    target = update.effective_message
    parts = _number_parts(_split_text(text))
    sent = 0
    for part in parts:
        async def _do(p, **kw):
            await target.reply_text(p, **kw)
        if await _send_one_part(_do, part, markdown=markdown):
            sent += 1
    if sent < len(parts):
        print(f"[telegram] safe_reply : {sent}/{len(parts)} parties envoyées")


# ─── AUTORISATION ────────────────────────────────────────────────────────────
def authorized(update: Update) -> bool:
    return str(update.effective_chat.id) in CHAT_IDS
