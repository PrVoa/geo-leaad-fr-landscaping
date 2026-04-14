"""Gestion et rotation des proxies résidentiels (IPRoyal)."""

from __future__ import annotations

import logging
import random
import time

from config.settings import PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS, PROXY_COUNTRY, has_proxy

log = logging.getLogger("vao.proxy")


class ProxyManager:
    """
    Gère la rotation de proxy via IPRoyal.
    IPRoyal résidentiel avec rotation automatique par session :
    chaque connexion obtient une IP différente.
    On peut forcer une nouvelle IP en changeant le session ID.
    """

    def __init__(self):
        self._session_counter = 0
        self._last_ip: str | None = None

    def get_proxy_url(self) -> str | None:
        """Retourne l'URL proxy avec un session ID unique pour forcer la rotation."""
        if not has_proxy():
            return None

        self._session_counter += 1
        session_id = f"vao_{int(time.time())}_{self._session_counter}"

        # Format IPRoyal : user-country-fr-session-XXX
        user = f"{PROXY_USER}-country-{PROXY_COUNTRY}-session-{session_id}"
        url = f"http://{user}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"

        log.debug("Proxy session: %s", session_id)
        return url

    def get_playwright_proxy(self) -> dict | None:
        """Retourne le proxy formaté pour Playwright browser.new_context()."""
        url = self.get_proxy_url()
        if not url:
            return None
        return {"server": f"http://{PROXY_HOST}:{PROXY_PORT}",
                "username": f"{PROXY_USER}-country-{PROXY_COUNTRY}-session-vao_{int(time.time())}_{self._session_counter}",
                "password": PROXY_PASS}


# Singleton
_manager: ProxyManager | None = None


def get_proxy_manager() -> ProxyManager:
    global _manager
    if _manager is None:
        _manager = ProxyManager()
    return _manager
