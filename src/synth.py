"""Render a blueprint's Music component to audio, straight from its JSON.

Model (what castletool writes and Castle's "tone" sampler does):
  * song.tracks[].instrument.sample = {type:"tone", waveform, attack, release,
    amplitude.value, playbackRate.value}; props.volume / props.muted
  * song.tracks[].sequence = {startStep: {patternId}}
  * song.patterns[id].notes = {step: [{key, vel}]}
  * a step is 1/stepsPerBeat of a beat (4 per beat); tempo is the card's BPM
  * Castle's note `key` sounds one octave below the same MIDI number, which is
    why castletool adds 12 on import - so MIDI pitch = key - 12.
  * The sampler's `midiNote` is the instrument's root note (48 = castletool's
    default). Every track using that instrument is transposed by midiNote - 48
    semitones, so midiNote 60 sounds one octave higher than 48.
A tone has no sustain: it ramps up over `attack`, then dies away over `release`.

Drums: song.tracks[].instrument = {type:"drums", params:{kick, snare, ...}}; notes
use keys 36-42 (kick, low tom, high tom, snare, clap, hat, open hat) and have no
pitch, so each key renders its own little synthesized hit instead of a tone.
"""
from __future__ import annotations

import math

import numpy as np

SAMPLE_RATE = 22050
STEPS_PER_BEAT = 4

# loudness balance between waveforms (square/saw are hotter than sine)
WAVE_GAIN = {"sine": 0.55, "square": 0.30, "sawtooth": 0.34, "noise": 0.38}


DEFAULT_MIDI_NOTE = 48


def _midi_note_shift(sample: dict) -> float:
    """Semitones the instrument's root note (`midiNote`) moves its notes from the default."""
    try:
        return float(sample.get("midiNote", DEFAULT_MIDI_NOTE)) - DEFAULT_MIDI_NOTE
    except (TypeError, ValueError):
        return 0.0


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


# Castle drum keys -> (params group, use-flag) in instrument.params
DRUM_KEYS = {
    36: ("kick", "useKick"), 37: ("loTom", "useLoTom"), 38: ("hiTom", "useHiTom"),
    39: ("snare", "useSnare"), 40: ("clap", "useClap"),
    41: ("closedHat", "useClosedHat"), 42: ("openHat", "useOpenHat"),
}
DRUM_GAIN = 0.7


def _exp_env(n: int, decay: float, sr: int) -> np.ndarray:
    return np.exp(-np.arange(n) / (max(0.005, decay) * sr / 5.0))


def _hat(p: dict, n: int, rng, sr: int) -> np.ndarray:
    noise = rng.uniform(-1, 1, n + 1)
    hiss = np.diff(noise)                                   # crude high-pass
    f = 3000.0 + float(p.get("freq", 0.9)) * 5000.0
    t = np.arange(n) / sr
    metal = sum(np.sign(np.sin(2 * math.pi * f * r * t)) for r in (1.0, 1.4, 1.71)) / 3.0
    return (hiss * 0.5 + metal * float(p.get("body", 0.1)) * 2.0) * _exp_env(n, float(p.get("decay", 0.1)), sr)


def drum_buffer(voice: dict, key: int, sr: int, seed: int = 1337) -> np.ndarray:
    """One hit (unit velocity) of Castle drum piece `key` (36-42)."""
    group_flag = DRUM_KEYS.get(int(key))
    params = voice.get("params") or {}
    if not group_flag:
        return np.zeros(int(0.05 * sr), dtype=np.float32)
    group, flag = group_flag
    if not params.get(flag, True):
        return np.zeros(int(0.05 * sr), dtype=np.float32)
    p = params.get(group) or {}
    decay = float(p.get("decay", 0.2))
    n = max(int(0.05 * sr), int(decay * 1.3 * sr))
    t = np.arange(n) / sr
    rng = np.random.default_rng(seed + int(key))
    if group == "kick":
        f0 = float(p.get("freq", 80.0)); sweep = float(p.get("sweep", 0.6))
        f = f0 * (1.0 + sweep * 4.0 * np.exp(-t * 40.0))
        sig = np.sin(2 * math.pi * np.cumsum(f) / sr) * _exp_env(n, decay, sr)
        click = rng.uniform(-1, 1, n) * np.exp(-t * 400.0) * float(p.get("punch", 0.9)) * 0.4
        sig = sig + click
    elif group in ("loTom", "hiTom"):
        f0 = 60.0 + float(p.get("freq", 0.75)) * 220.0
        if group == "hiTom":
            f0 *= 1.5
        f = f0 * (1.0 + 0.5 * np.exp(-t * 25.0))
        sig = np.sin(2 * math.pi * np.cumsum(f) / sr) * _exp_env(n, decay, sr)
    elif group == "snare":
        tam = float(p.get("tambre", 0.5))
        body = np.sin(2 * math.pi * (140.0 + float(p.get("freq", 0.5)) * 180.0) * t)
        rattle = np.diff(rng.uniform(-1, 1, n + 1))
        sig = (body * (1.0 - tam * 0.5) * 0.6 + rattle * (0.3 + tam * 0.5)) * _exp_env(n, decay, sr)
    elif group == "clap":
        raw = np.diff(rng.uniform(-1, 1, n + 1))
        env = _exp_env(n, decay, sr)
        for off in (0.0, 0.011, 0.022):                      # 3 quick bursts, then a tail
            i = int(off * sr)
            env[i:i + int(0.008 * sr)] += 0.8
        sig = raw * env * 0.5
    else:                                                    # closedHat / openHat
        sig = _hat(p, n, rng, sr)
    peak = float(np.max(np.abs(sig))) if len(sig) else 0.0
    if peak > 1.0:                                           # stacked parts (kick click, clap bursts)
        sig = sig / peak
    return (sig * DRUM_GAIN).astype(np.float32)


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
        if inst.get("type") == "drums":
            voices.append({
                "drums": True,
                "params": inst.get("params") or {},
                "vol": float(props.get("volume", 1.0)),
                "muted": bool(props.get("muted", False)),
            })
            continue
        if sample.get("type") not in (None, "tone"):
            warnings.append(f"Track {ti + 1}: '{sample.get('type')}' samples can't be previewed; using a sine tone.")
        voices.append({
            "wave": sample.get("waveform") or "sine",
            "attack": float(sample.get("attack") or 0.0),
            "release": float(sample.get("release") if sample.get("release") is not None else 0.3),
            "amp": float((sample.get("amplitude") or {}).get("value", 1.0)),
            "rate": float((sample.get("playbackRate") or {}).get("value", 1.0)),
            "shift": _midi_note_shift(sample),
            "vol": float(props.get("volume", 1.0)),
            "muted": bool(props.get("muted", False)),
        })
    return voices, warnings


def note_buffer(voice: dict, key: float, sr: int, seed: int = 1337) -> np.ndarray:
    """One note of this voice (unit velocity): oscillator x attack/release envelope."""
    if voice.get("drums"):
        return (drum_buffer(voice, int(key), sr, seed) * voice["vol"]).astype(np.float32)
    dur = max(0.05, voice["attack"] + voice["release"])
    n = int(dur * sr)
    rng = np.random.default_rng(seed + int(key))
    pitch = key + voice.get("shift", 0.0)
    sig = _tone(voice["wave"], _freq(pitch, voice["rate"]), n, rng, sr)
    sig = sig * _envelope(n, voice["attack"], voice["release"], sr)
    gain = WAVE_GAIN.get(voice["wave"], 0.4) * voice["amp"] * voice["vol"]
    return (sig * gain).astype(np.float32)
