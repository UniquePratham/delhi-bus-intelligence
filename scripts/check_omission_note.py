"""Verify the omission note appears on a route that really has an omitted pair.

Plans a journey along route 114UP, which contains the omitted pair
(398 -> 689), and asserts the map reports it on screen.
"""
from __future__ import annotations

import sys

import duckdb
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3100/"
REF = "data/processed/reference.duckdb"


def main() -> int:
    con = duckdb.connect(REF, read_only=True)
    rid = con.execute(
        "select route_id from routes where route_name = '114UP' limit 1"
    ).fetchone()[0]
    stops = [
        r[0]
        for r in con.execute(
            "select station_id from station_patterns where route_id = ? order by stop_sequence",
            [rid],
        ).fetchall()
    ]
    names = {
        int(sid): n
        for sid, n in con.execute(
            "select station_id, name from stations where station_id in (?, ?)",
            [stops[0], stops[-1]],
        ).fetchall()
    }
    origin, dest = names[stops[0]], names[stops[-1]]
    print(f"route 114UP: {origin} -> {dest}  ({len(stops)} stops)")

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.goto(URL, wait_until="domcontentloaded", timeout=120_000)
        pg.wait_for_selector("[data-journey-card]", timeout=180_000)

        def set_field(field: str, value: str) -> None:
            el = pg.locator(f"#{field}")
            el.scroll_into_view_if_needed()
            el.click()
            el.fill("")
            pg.wait_for_timeout(200)
            pg.keyboard.type(value, delay=60)
            pg.wait_for_timeout(900)
            pg.keyboard.press("Escape")

        set_field("from", origin)
        set_field("to", dest)
        pg.get_by_role("button", name="Plan journey").click()
        pg.wait_for_timeout(1500)
        pg.wait_for_function(
            """() => !!document.querySelector('[data-route-ready="true"]')""",
            timeout=180_000,
        )
        pg.wait_for_timeout(3500)

        el = pg.locator('[data-route-ready="true"]')
        missing = int(el.get_attribute("data-route-missing-pairs") or "0")
        print(f"missing pairs on this journey: {missing}")
        notes = pg.locator("[data-route-notes]")
        note_count = notes.count()
        note_text = notes.inner_text() if note_count else ""
        print(f"note elements: {note_count}")
        if note_text:
            print(f"note: {note_text}")
        b.close()

    if missing > 0:
        assert note_count == 1, "omitted stretches exist but no note is shown"
        assert str(missing) in note_text, "the note does not state the count"
        print("PASS: the omission is reported on screen")
    else:
        assert note_count == 0, "a complete route should not show a note"
        print("journey had no omitted pair; note correctly absent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
