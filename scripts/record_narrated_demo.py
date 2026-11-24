"""Record the narrated demo: drive the site scene by scene, then mux.

Each scene in `docs/demo_timing.json` has a fixed hold time derived from its
narration. The recorder performs the scene's action, then waits out the
remainder of that hold, so the picture and the voice stay in step without any
manual editing.

Output: `docs/demo.mp4` with narration, a quiet music bed and burned-in
subtitles.

Usage:
    python scripts/record_narrated_demo.py [url]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TIMING = ROOT / "docs" / "demo_timing.json"
SRT = ROOT / "docs" / "demo.srt"
OUT = ROOT / "docs" / "demo.mp4"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3100/"
W, H = 1600, 900


def smooth_scroll(page, to: float, ms: int = 800) -> None:
    """Scroll to an absolute offset in steps, then confirm it landed.

    A requestAnimationFrame loop is unreliable in headless Chromium (rAF is
    throttled when the page is not foregrounded), which silently left earlier
    takes stuck at the top of the page while the narration moved on. Stepping
    with explicit waits and asserting the final position cannot fail quietly.
    """
    start = page.evaluate("window.scrollY")
    # Clamp to what the document can actually scroll to. The evidence section
    # grows as its charts mount, so a target measured a moment earlier can be
    # past the current bottom; scrolling there silently stops short.
    max_y = page.evaluate(
        "() => document.documentElement.scrollHeight - window.innerHeight"
    )
    to = max(0.0, min(float(to), float(max_y)))
    steps = max(1, int(ms / 40))
    for i in range(1, steps + 1):
        y = start + (to - start) * (i / steps)
        page.evaluate("(y) => window.scrollTo(0, y)", y)
        page.wait_for_timeout(40)
    page.evaluate("(y) => window.scrollTo(0, y)", to)
    page.wait_for_timeout(120)
    landed = page.evaluate("window.scrollY")
    if abs(landed - to) > 4:
        raise RuntimeError(f"scroll to {to:.0f} landed at {landed:.0f}")


def section_y(page, text: str, offset: int = -110) -> float:
    y = page.evaluate(
        """([text, offset]) => {
            const els = [...document.querySelectorAll('h2, h3')];
            const el = els.find((e) => e.textContent && e.textContent.includes(text));
            if (!el) return null;
            return el.getBoundingClientRect().top + window.scrollY + offset;
        }""",
        [text, offset],
    )
    if y is None:
        raise RuntimeError(f"section not found: {text}")
    return y


def main() -> int:
    timing = json.loads(TIMING.read_text(encoding="utf-8"))
    scenes = timing["scenes"]
    tmp = Path(tempfile.mkdtemp(prefix="demo-"))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--force-device-scale-factor=1"])
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            record_video_dir=str(tmp),
            record_video_size={"width": W, "height": H},
            device_scale_factor=1,
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=120_000)

        # The model must be ready before the first scene's clock starts, or the
        # narration runs over a loading bar.
        page.wait_for_selector("[data-journey-card]", timeout=180_000)
        page.wait_for_function(
            """() => !!document.querySelector('[data-route-ready="true"]')""",
            timeout=180_000,
        )
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1200)

        t0 = page.evaluate("performance.now()")

        def wait_until(scene_start_s: float) -> None:
            """Hold until the scene's narration window has elapsed."""
            elapsed = (page.evaluate("performance.now()") - t0) / 1000.0
            remain = scene_start_s - elapsed
            if remain > 0:
                page.wait_for_timeout(int(remain * 1000))

        for s in scenes:
            sid = s["id"]
            print(f"  {sid:<16} hold {s['hold_s']:5.2f}s", flush=True)

            if s["action"] == "hero":
                page.evaluate("window.scrollTo(0, 0)")

            elif s["action"] == "load":
                smooth_scroll(page, 560, 900)

            elif s["action"] == "autocomplete":
                page.evaluate("window.scrollTo(0, 560)")
                page.wait_for_timeout(250)

                def pick(field: str, typed: str, want: str) -> None:
                    """Type a prefix, then click the exact suggestion.

                    Clicking the option by name is deterministic; relying on
                    ArrowDown + Enter picks whatever happens to be first, which
                    is how an earlier take ended up planning to the wrong stop.
                    Typing goes through the keyboard rather than Locator.type so
                    a re-render mid-word cannot detach the element.
                    """
                    el = page.locator(f"#{field}")
                    el.scroll_into_view_if_needed()
                    el.click()
                    el.fill("")
                    page.wait_for_timeout(200)
                    page.keyboard.type(typed, delay=110)
                    page.wait_for_selector(f"#{field}-list", timeout=15_000)
                    page.wait_for_timeout(700)
                    page.locator(f"#{field}-list button", has_text=want).first.click()
                    page.wait_for_timeout(400)
                    assert page.locator(f"#{field}-list").count() == 0, (
                        f"{field} suggestion list stayed open"
                    )

                pick("from", "kash", "Kashmere Gate")
                pick("to", "nehru pl", "Nehru Place")

            elif s["action"] == "plan":
                page.get_by_role("button", name="Plan journey").click()
                page.wait_for_timeout(900)
                page.wait_for_function(
                    """() => !!document.querySelector('[data-route-ready="true"]')""",
                    timeout=120_000,
                )
                assert page.locator("[data-journey-card]").count() > 0, "no journey cards"

            elif s["action"] == "map":
                # Slow push in on the drawn route so the road alignment is
                # visible, then back out.
                page.mouse.move(1030, 620)
                for _ in range(3):
                    page.mouse.wheel(0, -220)
                    page.wait_for_timeout(700)
                page.wait_for_timeout(2500)
                for _ in range(3):
                    page.mouse.wheel(0, 220)
                    page.wait_for_timeout(700)

            elif s["action"] == "mode":
                page.get_by_role("button", name="2d", exact=True).click()
                page.wait_for_timeout(2200)
                page.get_by_role("button", name="3d", exact=True).click()

            elif s["action"] == "pref":
                page.get_by_role("button", name="Calmer", exact=True).click()
                page.wait_for_timeout(2400)
                page.get_by_role("button", name="Fastest", exact=True).click()

            elif s["action"] == "hour":
                smooth_scroll(page, section_y(page, "A trip is not one number"), 900)
                page.wait_for_timeout(1400)
                box = page.locator("svg[role='img']").first.bounding_box()
                if box:
                    x = box["x"] + box["width"] * 0.44
                    y = box["y"] + box["height"] * 0.5
                    page.mouse.move(x, y)
                    page.wait_for_timeout(600)
                    page.mouse.click(x, y)

            elif s["action"] == "crowd":
                smooth_scroll(page, section_y(page, "Where the bus fills up"), 900)
                page.wait_for_timeout(1200)
                route = page.locator("#route")
                route.click()
                route.fill("")
                route.type("274AUP", delay=90)
                page.wait_for_timeout(1600)
                page.locator("#crowd-hour").fill("9")

            elif s["action"] == "evidence":
                smooth_scroll(page, section_y(page, "Measured, not claimed"), 900)
                page.wait_for_timeout(2600)
                smooth_scroll(page, page.evaluate("document.body.scrollHeight"), 1400)

            elif s["action"] == "close":
                smooth_scroll(page, 0, 1200)

            wait_until(s["end_s"])

        video = page.video
        ctx.close()
        browser.close()
        raw = Path(video.path())
        shutil.copy(raw, tmp / "raw.webm")
        print(f"webm {raw.stat().st_size/1e6:.1f} MB", flush=True)

    # Narration: concatenate the per-scene WAVs with the same holds the recorder
    # used, so audio and picture share one clock.
    audio = tmp / "voice.wav"
    parts = []
    for s in scenes:
        parts.append(f"file '{ROOT / s['audio']}'")
        parts.append(f"duration {s['hold_s']}")
    (tmp / "voice.txt").write_text("\n".join(parts), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(tmp / "voice.txt"),
         "-c:a", "pcm_s16le", str(audio)],
        check=True, capture_output=True,
    )

    # A quiet music bed: two detuned sine pads with a slow tremolo, so the
    # narration is never sitting on silence but the bed never competes with it.
    bed = tmp / "bed.wav"
    total = timing["total_s"]
    subprocess.run(
        ["ffmpeg", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=110:duration={total}",
         "-f", "lavfi", "-i", f"sine=frequency=164.81:duration={total}",
         "-f", "lavfi", "-i", f"sine=frequency=220:duration={total}",
         "-filter_complex",
         "[0:a]volume=0.30[a];[1:a]volume=0.18[b];[2:a]volume=0.10[c];"
         "[a][b][c]amix=inputs=3:duration=longest,"
         "tremolo=f=0.12:d=0.35,lowpass=f=900,volume=0.16[out]",
         "-map", "[out]", "-c:a", "pcm_s16le", str(bed)],
        check=True, capture_output=True,
    )

    # Burn the subtitles in and mux the audio.
    srt_escaped = str(SRT).replace("\\", "/").replace(":", "\\:")
    vf = (
        f"scale={W}:{H}:flags=lanczos,fps=30,"
        f"subtitles='{srt_escaped}':force_style="
        "'FontName=Segoe UI,FontSize=17,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=1.6,Shadow=0,"
        "MarginV=34,Alignment=2'"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(tmp / "raw.webm"),
        "-i", str(audio),
        "-i", str(bed),
        "-filter_complex",
        f"[0:v]{vf}[v];[1:a][2:a]amix=inputs=2:duration=first:weights=1 0.5[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "slow", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "160k", "-shortest",
        str(OUT),
    ]
    print("encoding", flush=True)
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"mp4 {OUT}  {OUT.stat().st_size/1e6:.1f} MB", flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
