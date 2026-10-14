"""Render a blueprint's Music component to audio, straight from its JSON.

Model (what castletool writes and Castle's "tone" sampler does):
  * song.tracks[].instrument.sample = {type:"tone", waveform, attack, release,
    amplitude.value, playbackRate.value}; props.volume / props.muted
  * song.tracks[].sequence = {startStep: {patternId}}
  * song.patterns[id].notes = {step: [{key, vel}]}
  * a step is 1/stepsPerBeat of a beat (4 per beat); tempo is the card's BPM
  * Castle's note `key` sounds one octave below the same MIDI number, which is
    why castletool adds 12 on import - so MIDI pitch = key - 12.
A tone has no sustain: it ramps up over `attack`, then dies away over `release`.
"""
from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 22050
STEPS_PER_BEAT = 4

# loudness balance between waveforms (square/saw are hotter than sine)
WAVE_GAIN = {"sine": 0.55, "square": 0.30, "sawtooth": 0.34, "noise": 0.38}


def _freq(key: float, rate: float = 1.0) -> float:
    return 440.0 * 2.0 ** ((key - 12 - 69) / 12.0) * rate


def _tone(waveform: str, freq: float, n: int, rng, sr: int) -> np.ndarray:
    t = np.arange(n) / sr
    if waveform == "square":
        return np.sign(np.sin(2 * math.pi * freq * t)) + 0.0
    if waveform == "sawtooth":
        return 2.0 * ((freq * t) % 1.0) - 1.0
    if waveform == "noise":
        # sample-and-hold noise: low keys rumble, high keys hiss
        hold = max(1, int(sr / min(sr / 2, max(60.0, freq * 6))))
        raw = rng.uniform(-1, 1, n // hold + 2)
        return np.repeat(raw, hold)[:n]
    return np.sin(2 * math.pi * freq * t)  # sine / unknown


def _envelope(n: int, attack: float, release: float, sr: int) -> np.ndarray:
    a = int(attack * sr)
    env = np.ones(n)
    if a > 0:
        env[:a] = np.linspace(0, 1, min(a, n), endpoint=False)
    r0 = a
    rn = max(1, n - r0)
    env[r0:] = (1 - np.arange(rn) / rn) ** 2   # fast-then-slow decay
    return env


def step_seconds(bpm: float) -> float:
    return 60.0 / (max(1.0, bpm) * STEPS_PER_BEAT)


def collect_notes(music: dict, bpm: float) -> list[dict]:
    """Flatten the song into [{t, key, vel, track}] (seconds)."""
    song = (music or {}).get("song") or {}
    patterns = song.get("patterns") or {}
    sec = step_seconds(bpm)
    out = []
    for ti, track in enumerate(song.get("tracks") or []):
        for start_key, ref in (track.get("sequence") or {}).items():
            pat = patterns.get((ref or {}).get("patternId"))
            if not pat:
                continue
            try:
                start = float(start_key)
            except ValueError:
                continue
            for step_key, notes in (pat.get("notes") or {}).items():
                try:
                    step = float(step_key)
                except ValueError:
                    continue
                for nt in notes or []:
                    out.append({"t": (start + step) * sec, "key": nt.get("key", 60),
                                "vel": nt.get("vel", 100), "track": ti})
    out.sort(key=lambda n: n["t"])
    return out


def music_stats(music: dict, bpm: float) -> dict:
    notes = collect_notes(music, bpm)
    song = (music or {}).get("song") or {}
    return {
        "tracks": len(song.get("tracks") or []),
        "patterns": len(song.get("patterns") or {}),
        "notes": len(notes),
        "seconds": (notes[-1]["t"] if notes else 0.0),
        "bpm": bpm,
    }


def voices_from_music(music: dict) -> tuple[list[dict], list[str]]:
    """Per-track oscillator settings read from the Music JSON, plus warnings."""
    tracks = ((music or {}).get("song") or {}).get("tracks") or []
    warnings: list[str] = []
    voices = []
    for ti, track in enumerate(tracks):
        inst = track.get("instrument") or {}
        sample = inst.get("sample") or {}
        props = inst.get("props") or {}
        if sample.get("type") not in (None, "tone"):
            warnings.append(f"Track {ti + 1}: '{sample.get('type')}' samples can't be previewed; using a sine tone.")
        voices.append({
            "wave": sample.get("waveform") or "sine",
            "attack": float(sample.get("attack") or 0.0),
            "release": float(sample.get("release") if sample.get("release") is not None else 0.3),
            "amp": float((sample.get("amplitude") or {}).get("value", 1.0)),
            "rate": float((sample.get("playbackRate") or {}).get("value", 1.0)),
            "vol": float(props.get("volume", 1.0)),
            "muted": bool(props.get("muted", False)),
        })
    return voices, warnings


def note_buffer(voice: dict, key: float, sr: int, seed: int = 1337) -> np.ndarray:
    """One note of this voice (unit velocity): oscillator x attack/release envelope."""
    dur = max(0.05, voice["attack"] + voice["release"])
    n = int(dur * sr)
    rng = np.random.default_rng(seed + int(key))
    sig = _tone(voice["wave"], _freq(key, voice["rate"]), n, rng, sr)
    sig = sig * _envelope(n, voice["attack"], voice["release"], sr)
    gain = WAVE_GAIN.get(voice["wave"], 0.4) * voice["amp"] * voice["vol"]
    return (sig * gain).astype(np.float32)
