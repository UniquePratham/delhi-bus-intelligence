"""Generate narration audio and an SRT subtitle track from demo_script.json.

Uses the Windows SAPI voices through System.Speech, so no network call and no
extra dependency. Each scene becomes one WAV; the durations are written to
`docs/demo_timing.json` so the recorder can hold each shot for exactly as long
as its narration.

Usage:
    python scripts/make_narration.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_script.json"
OUT = ROOT / "data" / "artifacts" / "narration"
TIMING = ROOT / "docs" / "demo_timing.json"


def synth(voice: str, rate: int, text: str, wav: Path) -> None:
    """Synthesise one line to a WAV file via PowerShell System.Speech."""
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SelectVoice('{voice}'); "
        f"$s.Rate = {rate}; "
        f"$s.SetOutputToWaveFile('{wav}'); "
        f"$s.Speak('{safe}'); "
        "$s.Dispose()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        check=True,
        capture_output=True,
    )


def duration(wav: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(wav),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> int:
    cfg = json.loads(SCRIPT.read_text(encoding="utf-8"))
    voice = cfg["voice"]
    rate = int(cfg.get("rate", 0))
    OUT.mkdir(parents=True, exist_ok=True)

    timing = []
    t = 0.0
    for scene in cfg["scenes"]:
        wav = OUT / f"{scene['id']}.wav"
        synth(voice, rate, scene["text"], wav)
        d = duration(wav)
        # A short beat after each line so the cut does not feel clipped.
        hold = d + 0.55
        timing.append(
            {
                "id": scene["id"],
                "action": scene["action"],
                "caption": scene["caption"],
                "audio": str(wav.relative_to(ROOT)).replace("\\", "/"),
                "speech_s": round(d, 3),
                "hold_s": round(hold, 3),
                "start_s": round(t, 3),
                "end_s": round(t + hold, 3),
            }
        )
        t += hold
        print(f"{scene['id']:<16} speech {d:6.2f}s  hold {hold:6.2f}s  at {t:7.2f}s")

    TIMING.write_text(json.dumps({"total_s": round(t, 3), "scenes": timing}, indent=2), encoding="utf-8")

    # Subtitles: the caption, shown for the whole scene.
    lines = []
    for i, s in enumerate(timing, 1):
        lines.append(str(i))
        lines.append(f"{srt_time(s['start_s'])} --> {srt_time(s['end_s'])}")
        lines.append(s["caption"])
        lines.append("")
    (ROOT / "docs" / "demo.srt").write_text("\n".join(lines), encoding="utf-8")

    print(f"\ntotal {t:.1f}s -> {TIMING}")
    print(f"subtitles -> {ROOT / 'docs' / 'demo.srt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
