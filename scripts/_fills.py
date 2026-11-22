"""List the fill layers and their paint so the light theme can target them."""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("[data-route-ready='true']", timeout=180_000)
    page.wait_for_timeout(10000)

    info = page.evaluate(
        """() => {
        const m = window.__map;
        if (!m) return { noMap: true };
        const out = [];
        for (const l of m.getStyle().layers) {
          if (l.type !== 'fill') continue;
          let color = null;
          try { color = m.getPaintProperty(l.id, 'fill-color'); } catch (e) {}
          out.push({ id: l.id, src: l['source-layer'], color: JSON.stringify(color).slice(0, 80) });
        }
        return { theme: document.documentElement.dataset.theme, fills: out };
      }"""
    )
    print("theme:", info.get("theme"))
    for f in info.get("fills", []):
        print(f)
    browser.close()
