"""Capture the live site at several zooms for honest review."""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("[data-route-ready='true']", timeout=180_000)
    page.wait_for_timeout(12000)

    page.screenshot(path="data/artifacts/live_default.png", full_page=False)

    info = page.evaluate(
        """() => {
        const el = document.querySelector('[data-route-ready]');
        return {
          segments: el.dataset.routeSegments,
          missing: el.dataset.routeMissingPairs,
          gaps: el.dataset.routeLinkGaps,
          maxGap: el.dataset.routeMaxGapM,
          below: el.dataset.routeBelowBuildings,
        };
      }"""
    )
    print("route state:", info)
    browser.close()
