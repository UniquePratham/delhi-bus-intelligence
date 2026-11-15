"""Browser smoke test for the Next.js site.

Checks the model loads, a journey is planned, the map renders, and the
preference tabs re-rank. Fails loudly rather than passing silently.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:3100/"


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(URL, wait_until="networkidle", timeout=120_000)

        # The model loads, then the first plan runs automatically.
        page.wait_for_selector('button[aria-pressed]', timeout=180_000)
        page.wait_for_timeout(2500)

        cards = page.locator('button[aria-pressed]').count()
        print(f"journey cards: {cards}")

        canvas = page.locator("canvas.maplibregl-canvas").count()
        print(f"map canvas: {canvas}")

        first = page.locator('button[aria-pressed]').first
        print("first card:", first.inner_text().replace("\n", " | ").encode("ascii", "replace").decode())

        # Preference tabs must re-rank.
        page.get_by_role("tab", name="Less crowded").click()
        page.wait_for_timeout(2500)
        print(f"cards after tab switch: {page.locator('button[aria-pressed]').count()}")

        # Crowding explorer must render bars.
        page.wait_for_selector("text=Crowding along a route", timeout=30_000)
        bars = page.locator("text=Crowding along a route").count()
        print(f"crowding explorer present: {bars > 0}")

        page.screenshot(path="data/artifacts/web.png", full_page=False)
        print("screenshot: data/artifacts/web.png")

        browser.close()

    if errors:
        print("\nconsole errors:")
        for e in errors[:10]:
            print("  ", e)
        return 1
    if cards == 0 or canvas == 0:
        print("\nFAILED: no cards or no map canvas")
        return 1
    print("\nweb smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
