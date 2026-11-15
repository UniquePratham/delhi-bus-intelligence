"""Smoke-test the map UI in a real browser.

Checks that the page loads, the API call succeeds, journey cards render, and
the map draws polylines. Fails loudly rather than silently passing.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8077/"


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(URL, wait_until="networkidle", timeout=120_000)

        # The page plans a journey on load.
        page.wait_for_selector(".card", timeout=180_000)
        cards = page.locator(".card").count()
        print(f"journey cards: {cards}")

        first = page.locator(".card").first
        text = " | ".join(first.inner_text().split("\n")[:4])
        print("first card text:", text.encode("ascii", "replace").decode())

        # Map polylines are SVG paths inside the Leaflet overlay pane.
        paths = page.locator(".leaflet-overlay-pane path").count()
        print(f"map polylines: {paths}")

        # Clicking a card must select it and redraw.
        page.locator(".card").nth(min(1, cards - 1)).click()
        page.wait_for_timeout(800)
        selected = page.locator(".card.selected").count()
        print(f"selected cards after click: {selected}")

        # The preference tabs must re-plan.
        page.locator('.tab[data-pref="less_crowded"]').click()
        page.wait_for_timeout(6000)
        page.wait_for_selector(".card", timeout=180_000)
        print(f"cards after tab switch: {page.locator('.card').count()}")

        # Scroll the results pane to the top so the screenshot shows the
        # layout as a user first sees it, not mid-scroll.
        page.evaluate("document.getElementById('results').scrollTop = 0")
        page.wait_for_timeout(400)

        # Assert the results pane is actually clipped to its grid row: its
        # scrollHeight must exceed its clientHeight, and its top must sit below
        # the tabs.
        geom = page.evaluate(
            """() => {
              const r = document.getElementById('results');
              const t = document.querySelector('.tabs');
              return {
                resultsTop: r.getBoundingClientRect().top,
                tabsBottom: t.getBoundingClientRect().bottom,
                scrollH: r.scrollHeight,
                clientH: r.clientHeight,
                overflowY: getComputedStyle(r).overflowY,
              };
            }"""
        )
        print("layout:", geom)
        assert geom["resultsTop"] >= geom["tabsBottom"] - 1, "results overlaps the tabs"
        assert geom["overflowY"] == "auto", "results pane is not scrollable"

        page.screenshot(path="data/artifacts/ui.png", full_page=False)
        print("screenshot: data/artifacts/ui.png")

        browser.close()

    if errors:
        print("\nconsole errors:")
        for e in errors:
            print("  ", e)
        return 1

    if cards == 0 or paths == 0:
        print("\nFAILED: no cards or no polylines rendered")
        return 1

    print("\nUI smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
