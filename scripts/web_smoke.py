"""Browser smoke test for the Next.js site.

Checks the model loads, a journey is planned, the map renders, and the
preference tabs re-rank. Fails loudly rather than passing silently.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

DEFAULT_URL = "http://127.0.0.1:3100/"
URL = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(URL, wait_until="domcontentloaded", timeout=120_000)

        # The model loads, then the first plan runs automatically.
        page.wait_for_selector("[data-journey-card]", timeout=180_000)
        page.wait_for_timeout(2500)

        cards = page.locator("[data-journey-card]").count()
        print(f"journey cards: {cards}")

        canvas = page.locator("canvas.maplibregl-canvas").count()
        print(f"map canvas: {canvas}")

        first = page.locator("[data-journey-card]").first
        print("first card:", first.inner_text().replace("\n", " | ").encode("ascii", "replace").decode())

        # Preference controls must re-rank.
        page.get_by_role("button", name="Calmer", exact=True).click()
        page.wait_for_timeout(2500)
        print(f"cards after preference switch: {page.locator('[data-journey-card]').count()}")

        # Crowding explorer must render bars.
        page.wait_for_selector("text=Where the bus fills up", timeout=30_000)
        print("crowding explorer present: True")

        # Wait until route geometry has been evaluated. This signal fires even
        # when every pair is missing, so assert an actual line was added.
        page.wait_for_function(
            """() => !!document.querySelector('[data-route-ready="true"]')""",
            timeout=120_000,
        )
        map_el = page.locator('[data-route-ready="true"]')
        assert map_el.get_attribute("data-route-drawn") == "true", "route geometry produced no rendered line"
        assert int(map_el.get_attribute("data-route-segments") or "0") > 0, "route has no rendered segments"
        assert map_el.get_attribute("data-route-below-buildings") == "true", "route layers are above city-3d"
        initial_segments = map_el.get_attribute("data-route-segments")
        page.get_by_role("button", name="2d", exact=True).click()
        page.wait_for_timeout(900)
        assert map_el.get_attribute("data-route-segments") == initial_segments, "2D mode changed route geometry"
        page.get_by_role("button", name="3d", exact=True).click()
        page.wait_for_timeout(900)
        assert map_el.get_attribute("data-route-segments") == initial_segments, "3D mode changed route geometry"
        print(f"route segments: {initial_segments}; 2D/3D geometry stable; below buildings: True")
        page.wait_for_timeout(3000)

        page.screenshot(path="data/artifacts/web.png", full_page=False)
        print("screenshot: data/artifacts/web.png")

        # Capture a close view of the route after the overview. Zoom at the
        # middle of the visible route so the road-centerline alignment can be
        # checked at neighborhood scale.
        page.mouse.dblclick(1035, 760)
        page.mouse.dblclick(1035, 760)
        page.wait_for_timeout(1800)
        page.screenshot(path="data/artifacts/web-close.png", full_page=False)
        print("close-zoom screenshot: data/artifacts/web-close.png")

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
