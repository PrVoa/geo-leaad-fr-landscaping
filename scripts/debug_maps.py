import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async
except ImportError:
    from playwright_stealth import Stealth
    async def stealth_async(page):
        await Stealth().apply_stealth_async(page)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--lang=fr-FR"])
        ctx = await browser.new_context(locale="fr-FR", timezone_id="Europe/Paris")
        page = await ctx.new_page()
        await stealth_async(page)

        await page.goto("https://www.google.fr/maps/search/paysagiste+Lyon", wait_until="domcontentloaded")
        await asyncio.sleep(4)

        # Accepte cookies
        try:
            btn = page.locator("button:has-text('Tout accepter')").first
            if await btn.is_visible(timeout=3000):
                await btn.click()
                await asyncio.sleep(2)
        except: pass

        await asyncio.sleep(3)

        # Affiche les 10 premiers hrefs
        hrefs = await page.eval_on_selector_all(
            "a[href*='/maps/place/']",
            "els => els.map(e => e.href)"
        )
        print(f"\n{len(hrefs)} liens trouves. Voici les 5 premiers :\n")
        for h in hrefs[:5]:
            print(h[:200])
            print("---")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())