"""Config anti-détection pour Playwright."""

import random

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
]

# Délais humains (en ms)
DELAYS = {
    "between_keystrokes": (30, 100),
    "between_fields": (300, 800),
    "before_submit": (2000, 5000),
    "after_page_load": (1000, 3000),
    "between_prospects": (60_000, 120_000),
}


def get_stealth_context_options() -> dict:
    """Options pour browser.new_context() avec anti-détection."""
    return {
        "user_agent": random.choice(USER_AGENTS),
        "viewport": random.choice(VIEWPORTS),
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris",
        "permissions": [],
        "java_script_enabled": True,
        "bypass_csp": False,
        "ignore_https_errors": True,
    }


def random_delay(key: str) -> float:
    """Retourne un délai aléatoire en secondes pour la clé donnée."""
    lo, hi = DELAYS[key]
    return random.randint(lo, hi) / 1000.0
