"""Capture the Hansraj Gupta Marg area from the user's screenshot."""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
    page.wait_for_selector("[data-route-ready='true']", timeout=180_000)
    page.wait_for_timeout(10000)

    page.evaluate(
        """() => {
        const m = window.__map;
        if (!m) return;
        m.jumpTo({ center: [77.2405, 28.6444], zoom: 16.4, pitch: 45, bearing: -20 });
      }"""
    )
    page.wait_for_timeout(12000)
    page.screenshot(path="data/artifacts/building_check.png", full_page=False)
    print("saved")
    browser.close()
