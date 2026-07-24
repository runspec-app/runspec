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
    "host_up",
    "host_down",
    "room_up",
    "room_down",
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


def _sweep(
    f0: float,
    f1: float,
    dur: float,
    *,
    harmonics: tuple[float, ...] = (1.0,),
    attack: float = 0.008,
    decay: float = 3.0,
) -> list[float]:
    """An enveloped note whose pitch glides f0 → f1 (exponential glide, phase-
    accumulated so the slide is click-free). The 'power up / power down' shape."""
    n = int(SAMPLE_RATE * dur)
    norm = sum(abs(a) for a in harmonics) or 1.0
    out: list[float] = []
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        frac = i / max(n - 1, 1)
        freq = f0 * (f1 / f0) ** frac
        phase += 2.0 * math.pi * freq / SAMPLE_RATE
        env = (
            (t / attack)
            if t < attack
            else math.exp(-decay * (t - attack) / max(dur - attack, 1e-6))
        )
        val = 0.0
        for k, a in enumerate(harmonics, start=1):
            val += a * math.sin(phase * k)
        out.append((val / norm) * env)
    return out


def _mix(
    base: list[float], overlay: list[float], *, at: float = 0.0, gain: float = 1.0
) -> list[float]:
    """Overlay a second layer onto ``base`` starting ``at`` seconds in, padding
    the result to fit. Layering is what keeps the cues from sounding monotonic."""
    start = int(SAMPLE_RATE * at)
    out = list(base) + [0.0] * max(0, start + len(overlay) - len(base))
    for i, s in enumerate(overlay):
        out[start + i] += s * gain
    return out


def _seq(*segments: list[float]) -> list[float]:
    out: list[float] = []
    for seg in segments:
        out.extend(seg)
    return out


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
    """(Re)generate every bundled cue. Returns the written paths.

    The 0.200.0 set replaces the original single plinks with short layered
    motifs — sweeps, chords, and harmonic tails — so the cues read as one
    "console" voice rather than isolated beeps. Still fully synthesized here
    (no licensing exposure), still deliberately moderate in level.
    """
    written = [
        # confirm: rising two-note "ask" (A5 → E6) over a soft high shimmer
        # sweep — insistent but not harsh. The urgent cue.
        _write(
            "confirm",
            [
                _mix(
                    _seq(
                        _tone(880.0, 0.12, harmonics=(1.0, 0.25)),
                        _tone(1318.5, 0.30, harmonics=(1.0, 0.3, 0.12), decay=5.0),
                    ),
                    _sweep(1760.0, 2637.0, 0.30, decay=6.0),
                    at=0.10,
                    gain=0.18,
                )
            ],
        ),
        # success: rising major arpeggio (C6 → E6 → G6), each note overlapping
        # the last so it blooms instead of plinking.
        _write(
            "success",
            [
                _mix(
                    _mix(
                        _tone(1046.5, 0.34, harmonics=(1.0, 0.3), decay=6.0),
                        _tone(1318.5, 0.30, harmonics=(1.0, 0.3), decay=6.0),
                        at=0.09,
                    ),
                    _tone(1568.0, 0.34, harmonics=(1.0, 0.35, 0.12), decay=4.5),
                    at=0.18,
                )
            ],
        ),
        # error: low falling minor motif (G4 → Eb4) with a slow detuned beat
        # underneath — reads "wrong" without being a klaxon.
        _write(
            "error",
            [
                _mix(
                    _seq(
                        _tone(392.0, 0.16, harmonics=(1.0, 0.3)),
                        _tone(311.1, 0.36, harmonics=(1.0, 0.35, 0.15), decay=4.0),
                    ),
                    _tone(310.0, 0.36, harmonics=(1.0,), decay=4.0),
                    at=0.16,
                    gain=0.5,
                )
            ],
        ),
        # stopped: single damped note (A4) with a sub-octave thud — neutral full stop.
        _write(
            "stopped",
            [
                _mix(
                    _tone(440.0, 0.22, harmonics=(1.0, 0.2), decay=6.0),
                    _tone(220.0, 0.22, decay=6.0),
                    gain=0.4,
                )
            ],
        ),
        # trigger: quick grace-note into a bell ping (B4 → E5) with inharmonic
        # partials and a long tail — "something just fired".
        _write(
            "trigger",
            [
                _seq(
                    _tone(493.9, 0.07),
                    _tone(659.3, 0.42, harmonics=(1.0, 0.5, 0.0, 0.2), decay=4.5),
                )
            ],
        ),
        # reply: soft rising pair (D5 → F#5), gentle — the assistant is done.
        _write(
            "reply",
            [
                _mix(
                    _tone(587.3, 0.28, harmonics=(1.0, 0.2), decay=5.0),
                    _tone(740.0, 0.26, harmonics=(1.0, 0.15), decay=5.0),
                    at=0.10,
                    gain=0.8,
                )
            ],
        ),
        # message: warm two-note knock (C5 → E5) with rounder harmonics.
        _write(
            "message",
            [
                _seq(
                    _tone(523.3, 0.09, harmonics=(1.0, 0.3)),
                    _silence(0.02),
                    _tone(659.3, 0.22, harmonics=(1.0, 0.35, 0.1), decay=5.0),
                )
            ],
        ),
        # host_up: "power on" — upward sweep settling onto a confident A5.
        _write(
            "host_up",
            [
                _mix(
                    _sweep(330.0, 880.0, 0.30, harmonics=(1.0, 0.3), decay=2.0),
                    _tone(880.0, 0.30, harmonics=(1.0, 0.3, 0.1), decay=4.5),
                    at=0.26,
                )
            ],
        ),
        # host_down: "power down" — falling sweep with a sub layer. Distinct
        # from error: a slide, not a phrase.
        _write(
            "host_down",
            [
                _mix(
                    _sweep(880.0, 233.1, 0.44, harmonics=(1.0, 0.3), decay=2.2),
                    _sweep(440.0, 116.5, 0.44, decay=2.2),
                    gain=0.4,
                )
            ],
        ),
        # room_up: softer, higher cousin of host_up — chat link restored.
        _write(
            "room_up",
            [
                _mix(
                    _sweep(523.3, 1046.5, 0.24, decay=2.5),
                    _tone(1046.5, 0.26, harmonics=(1.0, 0.25), decay=5.0),
                    at=0.20,
                    gain=0.8,
                )
            ],
        ),
        # room_down: soft falling third (E5 → C5) over a low slide — chat link lost.
        _write(
            "room_down",
            [
                _mix(
                    _seq(
                        _tone(659.3, 0.12),
                        _tone(523.3, 0.26, harmonics=(1.0, 0.25), decay=4.5),
                    ),
                    _sweep(392.0, 196.0, 0.34, decay=2.5),
                    at=0.06,
                    gain=0.35,
                )
            ],
        ),
    ]
    return written


if __name__ == "__main__":
    for p in build():
        print(f"wrote {p.name} ({p.stat().st_size} bytes)")
