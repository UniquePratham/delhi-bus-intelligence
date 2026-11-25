"""Check the omission note does not overlap the map's other chrome.

The note sits bottom-centre; the crowding legend is bottom-left and the leg
detail panel is bottom-right. At a narrow viewport they can collide, which
reads as a rendering bug rather than a caption.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3100/"
SIZES = [(1600, 1000), (1280, 900), (1024, 800)]


def rects_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def main() -> int:
    failures = 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w, h in SIZES:
            pg = b.new_page(viewport={"width": w, "height": h})
            pg.goto(URL, wait_until="domcontentloaded", timeout=120_000)
            pg.wait_for_selector("[data-journey-card]", timeout=180_000)

            for field, value in (
                ("from", "Old Delhi Railway Station"),
                ("to", "Qutub Garh Village"),
            ):
                el = pg.locator(f"#{field}")
                el.scroll_into_view_if_needed()
                el.click()
                el.fill("")
                pg.wait_for_timeout(200)
                pg.keyboard.type(value, delay=50)
                pg.wait_for_timeout(900)
                pg.keyboard.press("Escape")

            pg.get_by_role("button", name="Plan journey").click()
            pg.wait_for_timeout(1500)
            pg.wait_for_function(
                """() => !!document.querySelector('[data-route-ready="true"]')""",
                timeout=180_000,
            )
            pg.wait_for_timeout(3000)

            note = pg.locator("[data-route-notes]")
            if note.count() == 0:
                print(f"{w}x{h}: no note (journey complete)")
                pg.close()
                continue

            nr = note.first.bounding_box()
            others = {
                "legend": pg.locator("text=Crowding").first.bounding_box(),
                "detail": pg.locator("text=This journey").first.bounding_box(),
            }
            bad = []
            for name, r in others.items():
                if r and nr and rects_overlap(nr, r):
                    bad.append(name)
            status = "OVERLAPS " + ", ".join(bad) if bad else "clear"
            print(f"{w}x{h}: note {nr['width']:.0f}x{nr['height']:.0f} -> {status}")
            if bad:
                failures += 1
            pg.close()
        b.close()

    print("PASS" if failures == 0 else f"FAIL: {failures} viewport(s) overlap")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
