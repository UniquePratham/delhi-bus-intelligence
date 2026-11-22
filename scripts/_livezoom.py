"""Capture the live site at close zoom to see the actual route."""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("[data-route-ready='true']", timeout=180_000)
    page.wait_for_timeout(14000)

    # Zoom into the middle of the route.
    page.evaluate(
        """() => {
        const el = document.querySelector('[data-route-ready]');
        const c = document.querySelector('canvas.maplibregl-canvas');
        const m = c && c.closest('.maplibregl-map');
        let inst = null;
        for (const k of Object.keys(m || {})) {
          const v = m[k];
          if (v && v.getSource && v.getStyle) inst = v;
        }
        if (!inst) return;
        const src = inst.getSource('rt');
        if (!src) return;
        const segs = src._data.geometry.coordinates;
        const seg = segs.reduce((a, b) => (b.length > a.length ? b : a));
        inst.jumpTo({ center: seg[Math.floor(seg.length / 2)], zoom: 15.5, pitch: 40, bearing: -20 });
      }"""
    )
    page.wait_for_timeout(12000)
    page.screenshot(path="data/artifacts/live_zoom.png", full_page=False)
    print("saved")
    browser.close()
