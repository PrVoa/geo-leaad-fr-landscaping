"""
crm_watcher.py — Écoute Supabase Realtime sur la table landscapers.
Détecte les UPDATE dont notes ou rappel_le a changé
et envoie une notification Telegram à Laurie.

Zéro appel Claude, zéro lecture de la base.
Lancé en tâche asyncio depuis main.py.

UNE SEULE connexion Realtime persistante, protégée par singleton.
Reconnexion avec backoff exponentiel (30s → 60s → 120s → … → 10 min).
"""

import asyncio

from core.shared import bot, BOT_TOKEN
from config import CHAT_ID_LAURIE, SUPABASE_URL, SUPABASE_ANON_KEY


# Singleton : empêche deux watchers concurrents de doubler la fuite.
_watcher_running = False

# 30s, 60s, 120s, 240s, 480s, puis plafonné à 600s (10 min).
_BACKOFF_SCHEDULE = [30, 60, 120, 240, 480, 600]


async def _notify_laurie(text: str):
    if not BOT_TOKEN or not CHAT_ID_LAURIE:
        return
    try:
        await bot().send_message(chat_id=CHAT_ID_LAURIE, text=text)
    except Exception as e:
        print(f"[crm_watcher] Erreur Telegram : {e}")


def _should_notify(old: dict, new: dict) -> bool:
    """True si notes ou rappel_le a réellement changé."""
    for field in ("notes", "rappel_le"):
        old_val = old.get(field) or ""
        new_val = new.get(field) or ""
        if str(old_val) != str(new_val):
            return True
    return False


def _format_message(record: dict) -> str:
    name     = record.get("name") or "?"
    notes    = record.get("notes") or ""
    rappel   = record.get("rappel_le") or ""
    parts = [f"CRM mise à jour : {name}"]
    if notes:
        parts.append(f"Notes : {notes[:200]}")
    if rappel:
        parts.append(f"Rappel le : {rappel}")
    return "\n".join(parts)


async def start_crm_watcher():
    """Point d'entrée — à appeler depuis main.py via asyncio.create_task."""
    global _watcher_running

    if _watcher_running:
        print("[crm_watcher] Watcher déjà actif — pas de 2e connexion ouverte.")
        return

    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        print("[crm_watcher] Supabase non configuré, watcher désactivé.")
        return

    try:
        from realtime import AsyncRealtimeClient
    except ImportError:
        print("[crm_watcher] Package 'realtime' manquant — watcher désactivé.")
        return

    _watcher_running = True
    print("[crm_watcher] Démarrage — 1 connexion Realtime persistante (singleton).")

    realtime_url = f"{SUPABASE_URL}/realtime/v1"
    attempt = 0

    try:
        while True:
            client = None
            connected_ok = False
            try:
                client = AsyncRealtimeClient(
                    realtime_url,
                    token=SUPABASE_ANON_KEY,
                )
                await client.connect()

                channel = client.channel("landscapers-updates")

                async def on_update(payload):
                    old = payload.get("old_record", {}) or {}
                    new = payload.get("record", {}) or payload.get("new", {})
                    if _should_notify(old, new):
                        msg = _format_message(new)
                        await _notify_laurie(msg)
                        print(f"[crm_watcher] Notif Laurie : {msg[:80]}")

                # La lib realtime invoque le callback en synchrone (sans await).
                # On lui donne un wrapper sync qui planifie la coroutine sur la
                # boucle courante — sinon « coroutine was never awaited ».
                def on_update_sync(payload):
                    asyncio.ensure_future(on_update(payload))

                await channel.on_postgres_changes(
                    event="UPDATE",
                    schema="public",
                    table="landscapers",
                    callback=on_update_sync,
                ).subscribe()

                print("[crm_watcher] Connecté — écoute des UPDATE sur landscapers")
                connected_ok = True
                attempt = 0  # reset du backoff après une connexion réussie

                # Heartbeat : si la connexion tombe silencieusement, on sort.
                while True:
                    await asyncio.sleep(60)
                    if not client.is_connected:
                        print("[crm_watcher] Connexion perdue (silencieusement)")
                        break

            except Exception as e:
                print(f"[crm_watcher] Erreur connexion : {e}")

            finally:
                # CRITIQUE : toujours fermer le client avant d'en recréer un,
                # sinon chaque reconnexion laisse une connexion orpheline côté
                # Supabase jusqu'au timeout TCP → cause de la fuite des 1014.
                if client is not None:
                    try:
                        await client.close()
                    except Exception as e:
                        print(f"[crm_watcher] Erreur fermeture client : {e}")

            delay = _BACKOFF_SCHEDULE[min(attempt, len(_BACKOFF_SCHEDULE) - 1)]
            attempt += 1
            state = "après succès" if connected_ok else f"tentative {attempt}"
            print(f"[crm_watcher] Reconnexion dans {delay}s ({state})")
            await asyncio.sleep(delay)

    finally:
        _watcher_running = False
