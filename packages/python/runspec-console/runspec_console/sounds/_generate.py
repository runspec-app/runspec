"""_generate.py — synthesize the bundled audio cue set (dev-time tool).

Runs with the Python standard library only (``wave`` + ``struct`` + ``math`` —
no numpy) and writes the committed ``*.wav`` files next to this module. The cues
are short, original synthesized tones (mono, 44.1 kHz, 16-bit PCM) with a brief
linear attack + exponential decay envelope so they don't click, normalised to a
deliberately moderate amplitude (~0.3 full-scale) — overall loudness is the
operating system's volume mixer's job (``winsound`` has no per-sound volume).

Because the tones are wholly synthesized here, the audio set is fully ours — no
licensing exposure. The ``.wav`` outputs are committed so ``pip install`` ships
them without anyone re-running this; regenerate by ``python -m
runspec_console.sounds._generate`` if you tweak the recipe below.

Run:
    python -m runspec_console.sounds._generate
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
PEAK = 0.3  # normalised full-scale fraction — moderate, not jarring

# The canonical cue names, shared with sounds.py (KNOWN_SOUNDS there mirrors
# these). Each maps to one entry in build() below.
KNOWN_SOUNDS = (
    "confirm",
    "success",
    "error",
    "stopped",
    "trigger",
    "reply",
    "message",
)


def _tone(
    freq: float,
    dur: float,
    *,
    harmonics: tuple[float, ...] = (1.0,),
    attack: float = 0.006,
    decay: float = 4.0,
) -> list[float]:
    """One enveloped note. ``harmonics`` are amplitudes for the 1st, 2nd, …
    partials; ``decay`` is the exponential decay rate over the note body."""
    n = int(SAMPLE_RATE * dur)
    norm = sum(abs(a) for a in harmonics) or 1.0
    out: list[float] = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = (
            (t / attack)
            if t < attack
            else math.exp(-decay * (t - attack) / max(dur - attack, 1e-6))
        )
        val = 0.0
        for k, a in enumerate(harmonics, start=1):
            val += a * math.sin(2.0 * math.pi * freq * k * t)
        out.append((val / norm) * env)
    return out


def _silence(dur: float) -> list[float]:
    return [0.0] * int(SAMPLE_RATE * dur)


def _write(name: str, segments: list[list[float]]) -> Path:
    data: list[float] = []
    for seg in segments:
        data.extend(seg)
    peak = max((abs(s) for s in data), default=1.0) or 1.0
    scale = PEAK / peak
    frames = bytearray()
    for s in data:
        v = int(max(-1.0, min(1.0, s * scale)) * 32767)
        frames += struct.pack("<h", v)
    path = Path(__file__).parent / f"{name}.wav"
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return path


def build() -> list[Path]:
    """(Re)generate every bundled cue. Returns the written paths."""
    written = [
        # confirm: rising two-tone "ask" (A5 → E6), insistent — the urgent cue.
        _write("confirm", [_tone(880.0, 0.13), _tone(1318.5, 0.20)]),
        # success: bright major third up (C6 → E6).
        _write(
            "success",
            [
                _tone(1046.5, 0.10, harmonics=(1.0, 0.3)),
                _tone(1318.5, 0.16, harmonics=(1.0, 0.3)),
            ],
        ),
        # error: low descending two-tone (G4 → C4).
        _write("error", [_tone(392.0, 0.14), _tone(261.6, 0.24)]),
        # stopped: short neutral blip.
        _write("stopped", [_tone(440.0, 0.13)]),
        # trigger: soft bell ping (added partials + longer decay tail).
        _write("trigger", [_tone(659.3, 0.30, harmonics=(1.0, 0.5, 0.25), decay=5.0)]),
        # reply: gentle single note (D5), softer than the others.
        _write("reply", [_tone(587.3, 0.20, harmonics=(1.0, 0.2))]),
        # message: two-note soft "knock".
        _write("message", [_tone(523.3, 0.08), _silence(0.02), _tone(659.3, 0.12)]),
    ]
    return written


if __name__ == "__main__":
    for p in build():
        print(f"wrote {p.name} ({p.stat().st_size} bytes)")
