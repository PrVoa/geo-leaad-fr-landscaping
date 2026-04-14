"""Gestion du lifecycle browser Playwright (async)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from config.settings import proxy_url


_playwright: Playwright | None = None
_browser: Browser | None = None


async def _get_browser() -> Browser:
    """Lance le browser Chromium (singleton)."""
    global _playwright, _browser
    if _browser is None or not _browser.is_connected():
        _playwright = await async_playwright().start()
        launch_args = {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
        px = proxy_url()
        if px:
            launch_args["proxy"] = {"server": px}
        _browser = await _playwright.chromium.launch(**launch_args)
    return _browser


@asynccontextmanager
async def new_context(stealth_options: dict | None = None) -> AsyncGenerator[BrowserContext, None]:
    """Crée un contexte browser isolé, le ferme automatiquement."""
    browser = await _get_browser()
    opts = stealth_options or {}
    ctx = await browser.new_context(**opts)
    try:
        yield ctx
    finally:
        await ctx.close()


@asynccontextmanager
async def new_page(stealth_options: dict | None = None) -> AsyncGenerator[Page, None]:
    """Crée une page dans un contexte isolé, ferme tout à la sortie."""
    async with new_context(stealth_options) as ctx:
        page = await ctx.new_page()
        try:
            yield page
        finally:
            await page.close()


async def shutdown() -> None:
    """Ferme proprement le browser et Playwright."""
    global _browser, _playwright
    if _browser and _browser.is_connected():
        await _browser.close()
    _browser = None
    if _playwright:
        await _playwright.stop()
    _playwright = None
