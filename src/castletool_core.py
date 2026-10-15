#!/usr/bin/env python3
"""
castle_tool.py — Interactive Castle deck editor
Streamlines injecting images/GIFs and MIDI into Castle blueprint JSON files.
"""

import base64
import bisect
import copy
import io
import json
import math
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path

CURRENT_VERSION = "0.6.0"
PYPI_URL = "https://pypi.org/pypi/castletool/json"

# ── optional deps ────────────────────────────────────────────────────────────
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import mido
    HAS_MIDO = True
except ImportError:
    HAS_MIDO = False

try:
    from fontTools.ttLib import TTFont
    from fontTools.pens.svgPathPen import SVGPathPen
    HAS_FONTTOOLS = True
except ImportError:
    HAS_FONTTOOLS = False

HAS_FFMPEG = shutil.which("ffmpeg") is not None

# pillow-heif is optional (HEIC/HEIF); installed on first use by ensure_heif().
HAS_HEIF = False
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAS_HEIF = True
except Exception:
    pass

# ── helpers ──────────────────────────────────────────────────────────────────

BOLD  = "\033[1m"
DIM   = "\033[2m"
RESET = "\033[0m"
WARN  = "\033[33m"
OK    = "\033[32m"
ERR   = "\033[31m"
BLUE  = "\033[34m"

def p(msg=""):        print(msg)
def pb(msg):          print(f"{BOLD}{msg}{RESET}")
def pw(msg):          print(f"{WARN}⚠  {msg}{RESET}")
def pe(msg):          print(f"{ERR}✗  {msg}{RESET}")
def ps(msg):          print(f"{OK}✓  {msg}{RESET}")

def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{BOLD}{prompt}{suffix}{RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        p(); sys.exit(0)
    return val if val else (default or "")

def yn(prompt, default="y"):
    hint = "Y/n" if default == "y" else "y/N"
    while True:
        try:
            ans = input(f"{BOLD}{prompt} ({hint}){RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            p(); sys.exit(0)
        if ans == "":
            return default == "y"
        if ans in ("y", "n"):
            return ans == "y"
        p("Please enter y or n.")

def choose(prompt, options):
    """Pick from a numbered list. Returns chosen item."""
    p()
    pb(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        raw = ask(f"Enter number (1-{len(options)})")
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        p("Invalid choice.")

def multi_choose(prompt, options):
    """
    Checklist: pick one or more options from a numbered list, then confirm.
    Returns the list of chosen items (in the order they appear in `options`).
    """
    p()
    pb(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}) {o}")
    while True:
        raw = ask(f"Enter numbers separated by spaces or commas (1-{len(options)})")
        parts = [x for x in raw.replace(",", " ").split() if x]
        if not parts or not all(x.isdigit() for x in parts):
            p("Enter one or more numbers from the list.")
            continue
        indices = sorted(set(int(x) for x in parts))
        if any(i < 1 or i > len(options) for i in indices):
            p("Invalid choice.")
            continue
        selected = [options[i - 1] for i in indices]
        p()
        pb("Selected:")
        for s in selected:
            print(f"  • {s}")
        if yn("Confirm this selection?", default="y"):
            return selected

def ask_path(prompt, default=None, search_dir: Path | None = None):
    """Plain file path input."""
    suffix = f" [{default}]" if default is not None else ""
    pb(prompt + suffix)
    try:
        val = input(f"{BOLD}Path: {RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        p(); sys.exit(0)
    return val if val else (default or "")

_HEX_COLOR_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")

def ask_color(prompt: str, default=(0, 0, 0)) -> list:
    """Ask for a color as RGB (0-255 each) or hex (#rrggbb). Returns [r, g, b, 1] as 0-1 floats."""
    while True:
        raw = ask(f"{prompt} (R G B 0-255, or hex #rrggbb)",
                   default=f"{default[0]} {default[1]} {default[2]}").strip()
        if _HEX_COLOR_RE.match(raw):
            hex_str = raw.lstrip("#")
            r, g, b = (int(hex_str[i:i+2], 16) / 255 for i in (0, 2, 4))
            return [round(r, 5), round(g, 5), round(b, 5), 1]
        parts = raw.replace(",", " ").split()
        if len(parts) == 3 and all(p.isdigit() for p in parts) and \
           all(0 <= int(p) <= 255 for p in parts):
            r, g, b = (int(p) / 255 for p in parts)
            return [round(r, 5), round(g, 5), round(b, 5), 1]
        pe("Enter three numbers 0-255 (e.g. 255 0 128) or hex (e.g. #ff0080)")


# ── character/codepoint spec parsing (shared by basic + advanced font UI) ────
# Grammar: whitespace-separated words. A word containing "U+"/"u+" must be
# exactly one U+XXXX codepoint token (codepoints must be separated by
# spaces -- gluing two together, e.g. "U+03A9U+0021", is malformed). Any
# other word is a run of literal characters, each one its own codepoint.
# A codepoint appearing more than once anywhere in the spec is a duplicate.

_CODEPOINT_RE = re.compile(r"[Uu]\+([0-9A-Fa-f]+)")

def parse_charspec_tokens(text: str):
    """
    Returns (tokens, seen_counts, valid).
    tokens: [(word, kind, codepoints_or_None), ...] in input order, where
      kind is "chars", "codepoint", or "malformed".
    seen_counts: codepoint -> total occurrences across the whole spec.
    valid: True only if there's at least one word, no malformed tokens,
      and no duplicate codepoints.
    """
    words = text.split()
    tokens = []
    seen_counts: dict[int, int] = {}
    for word in words:
        if "u+" in word.lower():
            m = _CODEPOINT_RE.fullmatch(word)
            if m:
                cp = int(m.group(1), 16)
                tokens.append((word, "codepoint", [cp]))
                seen_counts[cp] = seen_counts.get(cp, 0) + 1
            else:
                tokens.append((word, "malformed", None))
        else:
            cps = [ord(ch) for ch in word]
            tokens.append((word, "chars", cps))
            for cp in cps:
                seen_counts[cp] = seen_counts.get(cp, 0) + 1
    has_malformed = any(kind == "malformed" for _, kind, _ in tokens)
    has_dup = any(count > 1 for count in seen_counts.values())
    valid = bool(words) and not has_malformed and not has_dup
    return tokens, seen_counts, valid

def charspec_codepoints(text: str) -> list[int]:
    """Resolve a *valid* charspec string to a sorted, deduped codepoint list."""
    _tokens, seen_counts, _valid = parse_charspec_tokens(text)
    return sorted(seen_counts.keys())

def charspec_preview_parts(text: str):
    """[(fragment, color_or_None), ...] -- color is 'blue', 'red', or None."""
    tokens, seen_counts, _valid = parse_charspec_tokens(text)
    parts = []
    for i, (word, kind, cps) in enumerate(tokens):
        if i > 0:
            parts.append((" ", None))
        if kind == "malformed":
            parts.append((word, "red"))
        elif kind == "codepoint":
            parts.append((word, "red" if seen_counts[cps[0]] > 1 else "blue"))
        else:
            for ch, cp in zip(word, cps):
                parts.append((ch, "red" if seen_counts[cp] > 1 else "blue"))
    return parts

def charspec_preview_ansi(text: str) -> str:
    ansi = {"red": ERR, "blue": BLUE, None: ""}
    out = []
    for frag, color in charspec_preview_parts(text):
        out.append(f"{ansi[color]}{frag}{RESET}" if color else frag)
    return "".join(out)

def charspec_preview_rich(text: str) -> str:
    """Same preview, as Rich markup (for the Textual TUI)."""
    out = []
    for frag, color in charspec_preview_parts(text):
        esc = frag.replace("\\", "\\\\").replace("[", "\\[")
        out.append(f"[{color}]{esc}[/{color}]" if color else esc)
    return "".join(out)

def read_charspec_file(path: Path) -> str:
    """
    Read a charspec file: '--' starts a line comment (chosen specifically
    so it can't be triggered by accident), blank/comment-only lines and
    newlines are dropped, and everything else is joined with spaces.
    """
    cleaned = []
    for line in path.read_text(encoding="utf-8").splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        line = line.strip()
        if line:
            cleaned.append(line)
    return " ".join(cleaned)

def ask_charspec(prompt: str) -> str:
    """
    Loop until the user enters a valid character/codepoint spec, printing
    a colored preview each try (blue = recognized, red = duplicate or
    malformed). Returns the validated text.
    """
    while True:
        pb(prompt)
        try:
            text = input(f"{BOLD}> {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            p(); sys.exit(0)
        if not text:
            pe("Enter at least one character or codepoint.")
            continue
        print("  " + charspec_preview_ansi(text))
        _tokens, _seen, valid = parse_charspec_tokens(text)
        if valid:
            return text
        pe("Fix the highlighted characters/codepoints (duplicates or malformed U+.... tokens) and try again.")


def resolve_path(raw: str) -> Path:
    raw = raw.strip().strip("'\"")
    if raw.startswith("~/"):
        raw = str(Path.home()) + raw[1:]
    return Path(raw).expanduser().resolve()

# ── android / termux detection ───────────────────────────────────────────────

def is_termux() -> bool:
    return "com.termux" in str(Path.home()) or os.environ.get("TERMUX_VERSION") is not None

# ── update check ─────────────────────────────────────────────────────────────

def _version_tuple(v: str) -> tuple:
    parts = []
    for x in v.split("."):
        num = "".join(ch for ch in x if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts)

def get_installed_version() -> str:
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("castletool")
        except PackageNotFoundError:
            return CURRENT_VERSION
    except ImportError:
        return CURRENT_VERSION

def _wants_prereleases() -> bool:
    """CLI flag (--prerelease / --pre) or env var (CASTLETOOL_PRERELEASE)."""
    if any(a in ("--prerelease", "--pre") for a in sys.argv[1:]):
        return True
    return _env_flag("CASTLETOOL_PRERELEASE")

def _latest_version_from_pypi(data: dict, include_prereleases: bool) -> str:
    """info.version is PyPI's "latest stable" pointer and always skips
    pre-releases, so to consider them we have to look at the releases
    dict ourselves."""
    if not include_prereleases:
        return data["info"]["version"]

    releases = data.get("releases", {})
    candidates = [v for v, files in releases.items() if files]
    if not candidates:
        return data["info"]["version"]

    try:
        from packaging.version import parse
        candidates.sort(key=parse)
    except ImportError:
        candidates.sort(key=_version_tuple)
    return candidates[-1]

def check_for_update(include_prereleases: bool | None = None):
    """Best-effort check against PyPI. Never blocks or errors on failure."""
    if include_prereleases is None:
        include_prereleases = _wants_prereleases()

    current = get_installed_version()
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=3) as resp:
            data = json.loads(resp.read())
        latest = _latest_version_from_pypi(data, include_prereleases)
    except Exception:
        return

    if _version_tuple(latest) > _version_tuple(current):
        pw(f"A newer version is available: {latest} (you have {current})")
        if yn("Install it now?"):
            pip_argv = [sys.executable, "-m", "pip", "install", "--upgrade"]
            if include_prereleases:
                pip_argv.append("--pre")
            pip_argv.append("castletool")

            if os.name == "nt":
                # On Windows the running process (castletool.exe and its
                # imported modules) holds the files pip needs to replace,
                # so an in-process "pip install --upgrade castletool" can
                # partially delete/replace the package out from under
                # itself. Spawn a detached helper that waits for this PID
                # to exit before upgrading, so the files are unlocked
                # first.
                if _windows_deferred_update(pip_argv):
                    ps("Update will install after Castletool closes.")
                    sys.exit(0)
                else:
                    pw(
                        "Failed to automatically install, please run: "
                        + " ".join(pip_argv)
                    )
                    p()
            else:
                result = subprocess.run(pip_argv, capture_output=True, text=True)
                if result.returncode == 0:
                    ps("Updated! Please restart Castletool.")
                    sys.exit(0)
                else:
                    err = (result.stderr or result.stdout or "").strip()
                    pw(f"Update failed{(': ' + err) if err else ''}.")
                    p()
        else:
            p()


def _windows_deferred_update(pip_argv: list[str]) -> bool:
    """Spawn a detached helper that waits for this process to exit, then
    runs the upgrade. Returns True if the helper was launched, False if it
    could not be (caller should fall back to a manual-install message).
    """
    try:
        helper = (
            "import subprocess,sys,time\n"
            "pid=int(sys.argv[1])\n"
            "argv=sys.argv[2:]\n"
            "while True:\n"
            "    try:\n"
            "        subprocess.run(['tasklist','/FI',f'PID eq {pid}'],"
            " capture_output=True, text=True, check=True).stdout\n"
            "    except Exception:\n"
            "        break\n"
            "    out=subprocess.run(['tasklist','/FI',f'PID eq {pid}'],"
            " capture_output=True, text=True).stdout\n"
            "    if str(pid) not in out:\n"
            "        break\n"
            "    time.sleep(1)\n"
            "subprocess.run(argv)\n"
        )
        helper_path = Path(tempfile.gettempdir()) / "castletool_update_helper.py"
        helper_path.write_text(helper, encoding="utf-8")

        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [sys.executable, str(helper_path), str(os.getpid()), *pip_argv],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except Exception:
        return False

# ── dependency auto-install ──────────────────────────────────────────────────

def ensure_dependencies():
    """Best-effort auto-install of missing dependencies."""
    global HAS_PIL, HAS_MIDO, HAS_FONTTOOLS, HAS_FFMPEG, Image, mido, TTFont, SVGPathPen

    pip_missing = []
    if not HAS_PIL:  pip_missing.append("Pillow")
    if not HAS_MIDO: pip_missing.append("mido")
    if not HAS_FONTTOOLS: pip_missing.append("fonttools")

    if pip_missing:
        pb(f"Installing {', '.join(pip_missing)}...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *pip_missing])

    if not HAS_FFMPEG:
        pb("Installing ffmpeg...")
        if is_termux():
            subprocess.run(["pkg", "install", "-y", "ffmpeg"])
        elif shutil.which("apt-get"):
            subprocess.run(["sudo", "apt-get", "install", "-y", "ffmpeg"])
        elif shutil.which("brew"):
            subprocess.run(["brew", "install", "ffmpeg"])
        else:
            pw("Could not auto-install ffmpeg. Please install it manually.")

    if pip_missing or not HAS_FFMPEG:
        try:
            from PIL import Image as _Image
            Image = _Image
            HAS_PIL = True
        except ImportError:
            HAS_PIL = False
        try:
            import mido as _mido
            mido = _mido
            HAS_MIDO = True
        except ImportError:
            HAS_MIDO = False
        try:
            from fontTools.ttLib import TTFont as _TTFont
            from fontTools.pens.svgPathPen import SVGPathPen as _SVGPathPen
            TTFont, SVGPathPen = _TTFont, _SVGPathPen
            HAS_FONTTOOLS = True
        except ImportError:
            HAS_FONTTOOLS = False
        HAS_FFMPEG = shutil.which("ffmpeg") is not None

# ── deck / card / blueprint discovery ────────────────────────────────────────

def find_decks(search_dir: Path) -> list[Path]:
    """Return folders that look like Castle decks (contain deck.json)."""
    return sorted([d for d in search_dir.iterdir()
                   if d.is_dir() and (d / "deck.json").exists()])

def find_cards(deck: Path) -> list[Path]:
    cards_dir = deck / "cards"
    if not cards_dir.exists():
        return []
    return sorted([c for c in cards_dir.iterdir() if c.is_dir()])

def find_blueprints(card: Path) -> list[Path]:
    bp_dir = card / "scene" / "blueprints"
    if not bp_dir.exists():
        return []
    return sorted([f for f in bp_dir.glob("*.json")])

# ── card.json helpers ────────────────────────────────────────────────────────

def load_card_json(card: Path) -> dict:
    with open(card / "card.json", "r", encoding="utf-8") as f:
        return json.load(f)

def save_card_json(card: Path, data: dict):
    with open(card / "card.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def hex_to_rgba(hex_str: str) -> dict:
    """Convert a '#rrggbb' string to Castle's 0-1 float RGBA dict."""
    hex_str = hex_str.strip().lstrip("#")
    r = int(hex_str[0:2], 16) / 255
    g = int(hex_str[2:4], 16) / 255
    b = int(hex_str[4:6], 16) / 255
    return {"r": round(r, 5), "g": round(g, 5), "b": round(b, 5), "a": 1}

def do_edit_background_color(card: Path):
    card_data = load_card_json(card)
    current = card_data.get("backgroundColor", "#000000")
    p()
    pb(f"Current background color: {current}")
    while True:
        raw = ask("Enter new background color as hex", default=current).strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
            break
        pe("Enter a hex color like #09101a.")

    card_data["backgroundColor"] = raw
    card_data.setdefault("sceneProperties", {})["backgroundColor"] = hex_to_rgba(raw)
    save_card_json(card, card_data)
    ps(f"Background color set to {raw}.")

def set_card_tempo(card: Path, bpm: float):
    """Write the given BPM into card.json's sceneProperties.clock.tempo."""
    card_data = load_card_json(card)
    scene_props = card_data.setdefault("sceneProperties", {})
    clock = scene_props.setdefault("clock", {"beatsPerBar": 4, "stepsPerBeat": 4})
    clock["tempo"] = round(bpm, 2)
    save_card_json(card, card_data)

# ── image/gif → Drawing2 ─────────────────────────────────────────────────────

def load_image_frames(path: Path, width: int, height: int, ico_size=None,
                      smooth: bool = False):
    """Decode an image (or every frame of an animation) as PNG bytes resized to
    width×height. `smooth` = bilinear interpolation; off = crisp nearest-neighbor."""
    resample = Image.BILINEAR if smooth else Image.NEAREST
    img = Image.open(path)
    if ico_size:
        # .ico files embed several resolutions; select one before decoding.
        img.size = tuple(ico_size)

    # Normal image
    if not getattr(img, "is_animated", False):
        frame = img.convert("RGBA").resize((width, height), resample)
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        return [buf.getvalue()], 1.0

    # Animated GIF/WEBP
    frames = []
    durations = []

    try:
        while True:
            frame = img.convert("RGBA").resize((width, height), resample)
            buf = io.BytesIO()
            frame.save(buf, format="PNG")
            frames.append(buf.getvalue())
            durations.append(max(img.info.get("duration", 100), 1))
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    avg_ms = sum(durations) / len(durations)
    fps = round(1000 / avg_ms, 2)

    return frames, fps

def probe_video_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of a video file via ffprobe."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=s=x:p=0", str(path)],
        capture_output=True, text=True
    )
    raw = probe.stdout.strip()
    try:
        w, h = raw.split("x")
        return int(w), int(h)
    except Exception:
        return 64, 64


def extract_mp4_frames(path: Path, width: int, height: int, every_n: int = 1,
                       smooth: bool = False) -> tuple[list[bytes], float]:
    """Extract frames from MP4 using ffmpeg. Returns (png_frames, fps)."""
    if not HAS_FFMPEG:
        pe("ffmpeg not found. Install with: pkg install ffmpeg")
        sys.exit(1)

    # Get fps via ffprobe
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    raw_fps = probe.stdout.strip()
    try:
        num, den = raw_fps.split("/")
        fps = float(num) / float(den)
    except Exception:
        fps = 30.0

    effective_fps = fps / every_n

    # Extract frames as PNG via pipe. width/height already share the source's
    # aspect ratio (scaled), so a plain nearest-neighbor scale is exact — no
    # padding/cropping needed. `smooth` swaps nearest-neighbor for bilinear.
    scale_flags = "bilinear" if smooth else "neighbor"
    result = subprocess.run(
        ["ffmpeg", "-i", str(path),
         "-vf", f"select='not(mod(n\\,{every_n}))',scale={width}:{height}:flags={scale_flags}",
         "-vsync", "vfr",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True
    )

    # Split raw stdout into individual PNGs by PNG magic bytes
    raw = result.stdout
    png_magic = bytes([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a])
    frames = []
    start = 0
    while True:
        idx = raw.find(png_magic, start)
        if idx == -1:
            break
        next_idx = raw.find(png_magic, idx + 1)
        chunk = raw[idx:next_idx] if next_idx != -1 else raw[idx:]
        frames.append(chunk)
        start = next_idx if next_idx != -1 else len(raw)

    if not frames:
        pe("ffmpeg returned no frames. Is the file a valid MP4?")
        sys.exit(1)

    return frames, effective_fps


def probe_has_audio(path: Path) -> bool:
    """True if the video file has at least one audio stream."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True
    )
    return bool(probe.stdout.strip())


def extract_audio_mp3(path: Path, out_path: Path):
    """Extract the audio track to an MP3 (96k/44.1kHz/stereo) via ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(path), "-vn",
         "-c:a", "libmp3lame", "-b:a", "96k", "-ar", "44100", "-ac", "2",
         str(out_path)],
        check=True
    )


def probe_duration(path: Path) -> float | None:
    """Duration in seconds via ffprobe, or None if unknown."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    try:
        return float(probe.stdout.strip())
    except ValueError:
        return None


def split_audio_mp3(path: Path, out_dir: Path, is_video: bool) -> list[Path]:
    """Convert `path`'s sound to MP3 part(s) of at most AUDIO_CHUNK_SECONDS
    each, in playback order. An MP3 that's already short enough is returned
    untouched. Needs ffmpeg for anything else."""
    already_mp3 = (not is_video) and path.suffix.lower() == ".mp3"
    duration = probe_duration(path) if HAS_FFMPEG else None
    if already_mp3 and (not HAS_FFMPEG or (duration is not None and duration <= AUDIO_CHUNK_SECONDS)):
        return [path]
    if not HAS_FFMPEG:
        raise RuntimeError("ffmpeg is required to convert audio. Install with: pkg install ffmpeg")

    stem = "audio" if is_video else path.stem
    pattern = out_dir / f"{stem}_part%03d.mp3"
    codec = (["-c:a", "copy"] if already_mp3 else
             ["-c:a", "libmp3lame", "-b:a", "96k", "-ar", "44100", "-ac", "2"])
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(path), "-vn", *codec,
         "-f", "segment", "-segment_time", str(AUDIO_SEGMENT_SECONDS),
         "-reset_timestamps", "1", str(pattern)],
        check=True
    )
    parts = sorted(out_dir.glob(f"{stem}_part*.mp3"))
    if not parts:
        raise RuntimeError("ffmpeg produced no audio. Does the file have a sound track?")
    if len(parts) == 1:
        # Short enough for one piece: keep the plain, unsuffixed name.
        single = out_dir / f"{stem}.mp3"
        parts[0].rename(single)
        return [single]
    return parts


def get_castle_token() -> str:
    """Read the Castle CLI's saved login token from ~/.castle/config.json."""
    config_path = Path.home() / ".castle" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        token = config.get("token")
        if token:
            return token
    except (OSError, json.JSONDecodeError):
        pass
    raise RuntimeError("Castle login token wasn't found. Run: castle login")


CASTLE_API_URL = "https://api.castle.xyz/graphql"


def upload_audio_file(path: Path) -> dict:
    """Upload an MP3 to Castle via the uploadAudioFile GraphQL mutation.
    Returns {"fileId": ..., "url": ...}. Encodes the multipart/form-data
    request by hand since castletool has no dependency on `requests`."""
    token = get_castle_token()
    audio_bytes = path.read_bytes()

    operations = json.dumps({
        "query": """
          mutation UploadCastleAudio($file: Upload!) {
            uploadAudioFile(file: $file) {
              fileId
              url
            }
          }
        """,
        "variables": {"file": None},
    })
    field_map = json.dumps({"0": ["variables.file"]})

    boundary = uuid.uuid4().hex
    parts = []

    def add_field(name: str, value: str):
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n'.encode("utf-8")
        )

    add_field("operations", operations)
    add_field("map", field_map)
    parts.append(
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="0"; filename="{path.name}"\r\n'
        f'Content-Type: audio/mpeg\r\n\r\n'.encode("utf-8")
    )
    parts.append(audio_bytes)
    parts.append(f'\r\n--{boundary}--\r\n'.encode("utf-8"))
    body = b"".join(parts)

    req = urllib.request.Request(
        CASTLE_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-OS": "cli",
            "X-CLI-API-Version": "4",
            "X-Scene-Creator-Version": "latest",
            "X-Auth-Token": token,
            "Apollo-Require-Preflight": "true",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            response_text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        response_text = e.read().decode("utf-8", errors="replace")
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Castle returned HTTP {e.code}:\n{response_text[:500]}")
        raise RuntimeError(f"Castle returned HTTP {e.code}:\n{json.dumps(result, indent=2)}")

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Castle returned a non-JSON response:\n{response_text[:500]}")

    if result.get("error", {}).get("message"):
        raise RuntimeError(result["error"]["message"])
    if result.get("errors"):
        raise RuntimeError("\n".join(err["message"] for err in result["errors"]))

    uploaded = (result.get("data") or {}).get("uploadAudioFile")
    if not uploaded or not uploaded.get("url"):
        raise RuntimeError(f"Castle returned no audio URL:\n{json.dumps(result, indent=2)}")
    return uploaded


AUDIO_CHUNK_SECONDS = 30      # Castle rejects sounds longer than this
# Cut a hair under the limit: MP3 frames are ~26ms, and a cut lands on the next
# frame boundary, so a part cut at exactly 30s can come out at 30.01s.
AUDIO_SEGMENT_SECONDS = 29.9


def _play_sound_response(upload_url: str) -> dict:
    return {
        "name": "play sound",
        "behaviorId": 16,
        "params": {
            "type": "library",
            "playbackRate": 1, "amplitude": 1, "pan": 0,
            "recordingUrl": "", "uploadUrl": upload_url,
            "category": "random", "seed": 1337,
            "mutationSeed": 0, "mutationAmount": 5,
            "midiNote": 60, "waveform": "square",
            "attack": 0, "release": 0.4, "wait": True,
        },
    }


def create_audio_rule(upload_urls) -> dict:
    """A Rules-component rule that plays uploaded sound(s) on create and loops
    forever (mirrors Castle's own "play on create + loop" pattern). Given
    several URLs (a sound split into <=30s parts) the "play sound" responses
    are chained via "nextResponse" (nested inside each response's params, which
    is how Castle stores it; a sibling "next" key is ignored), each with "wait
    until sound ends" set, so the parts play back to back without overlapping."""
    if isinstance(upload_urls, str):
        upload_urls = [upload_urls]
    chain = None
    for url in reversed(upload_urls):
        resp = _play_sound_response(url)
        if chain is not None:
            resp["params"]["nextResponse"] = chain
        chain = resp
    return {
        "trigger": {"name": "create", "behaviorId": 16, "params": {}},
        "response": {
            "name": "infinite repeat",
            "behaviorId": 16,
            "params": {"interval": 0.01666, "body": chain},
        },
    }


def is_audio_loop_rule(rule: dict) -> bool:
    return (
        rule.get("trigger", {}).get("name") == "create"
        and rule.get("response", {}).get("name") == "infinite repeat"
        and rule.get("response", {}).get("params", {}).get("body", {}).get("name") == "play sound"
    )


def add_or_replace_audio_rule(actor: dict, upload_urls):
    components = actor["actorBlueprint"]["components"]
    rules_component = components.setdefault("Rules", {"rules": [], "disabled": False})
    rules_component.setdefault("rules", [])
    rules_component["disabled"] = False

    replacement = create_audio_rule(upload_urls)
    for i, rule in enumerate(rules_component["rules"]):
        if is_audio_loop_rule(rule):
            rules_component["rules"][i] = replacement
            break
    else:
        rules_component["rules"].append(replacement)


def quantize_frame(png_bytes: bytes, colors: int) -> bytes:
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    rgb = img.convert("RGB").quantize(colors=colors,
                                       method=Image.Quantize.MEDIANCUT).convert("RGB")
    result = Image.new("RGBA", img.size)
    result.paste(rgb)
    result.putalpha(img.split()[3])
    buf = io.BytesIO()
    result.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def build_drawing2(
    frames: list[bytes],
    fps: float,
    width: int,
    height: int,
    play_mode: str = "loop",
    sizes: list[tuple[int, int]] | None = None,
    scale: int | None = None,
) -> dict:
    """width/height is the overall (largest) size. `sizes` gives each frame's
    own pixel size when they differ; `scale` pins pixels-per-unit instead of
    deriving it from width/height (so several drawings share one density)."""
    # Empirical scale formula
    if scale is None:
        scale = max(1, round(max(width, height) / 20))

    def _bounds(w, h):
        return {"minX": -w / 2, "maxX": w / 2, "minY": -h / 2, "maxY": h / 2}

    half_w, half_h = width / 2, height / 2
    bounds = _bounds(width, height)
    frame_bounds = [_bounds(w, h) for w, h in sizes] if sizes else [bounds] * len(frames)

    castle_frames = [{
        "isLinked": False,
        "pathDataList": [],
        "fillImageBounds": fb,
        "fillPng": base64.b64encode(p).decode(),
        "avatarX": 0,
        "avatarY": 0,
        "avatarRadius": 5,
} for p, fb in zip(frames, frame_bounds)]

    return {
        "initialFrame": 1,
        "currentFrame": 1,
        "framesPerSecond": fps,
        "playMode": play_mode,
        "loopStartFrame": -1,
        "loopEndFrame": -1,
        "opacity": 1,
        "hash": str(abs(hash(frames[0])))[:19],
        "playing": False,
        "loop": play_mode != "still",

        "drawData": {
            "color": [1, 1, 1, 1],
            "lineColor": [0, 0, 0, 1],
            "gridSize": 0.71428,
            "scale": scale,
            "version": 3,
            "fillPixelsPerUnit": scale,
            "numTotalLayers": 1,
            "framesBounds": frame_bounds,
            "colors": [],
            "selectedFrame": 1,
            "layers": [{
                "title": "Layer 1",
                "id": "layer1",
                "isVisible": True,
                "isBitmap": True,
                "isAvatar": False,
                "frames": castle_frames,
        }],
        },

        "physicsBodyData": {
            "shapes": [{
                "p1": {"x": half_w / scale, "y": half_h / scale},
                "p2": {"x": -half_w / scale, "y": -half_h / scale},
                "p3": {"x": 0, "y": 0},
                "radius": 0,
                "x": 0,
                "y": 0,
                "type": "rectangle",
        }],
            "scale": scale,
            "version": 2,
            "zeroShapesInV1": False,
        },

        "disabled": False,
        } 

# ── svg → Drawing2 (vector) ──────────────────────────────────────────────────
# Merged from svg_to_castle.py: flattens all SVG paths to straight line
# segments (s:1, f:false). Bezier curves are sampled at `steps` intervals.

# Castle coordinate scale: SVG px → Castle units
# fillPixelsPerUnit = 25.6 by default
SVG_DEFAULT_PPU = 25.6
SVG_NS = "http://www.w3.org/2000/svg"


def _svg_lerp(a, b, t): return a + (b - a) * t

def _svg_quadratic_point(p0, p1, p2, t):
    x = (1-t)**2*p0[0] + 2*(1-t)*t*p1[0] + t**2*p2[0]
    y = (1-t)**2*p0[1] + 2*(1-t)*t*p1[1] + t**2*p2[1]
    return (x, y)

def _svg_cubic_point(p0, p1, p2, p3, t):
    x = (1-t)**3*p0[0] + 3*(1-t)**2*t*p1[0] + 3*(1-t)*t**2*p2[0] + t**3*p3[0]
    y = (1-t)**3*p0[1] + 3*(1-t)**2*t*p1[1] + 3*(1-t)*t**2*p2[1] + t**3*p3[1]
    return (x, y)


def _svg_arc_to_lines(x1, y1, rx, ry, x_rot, large, sweep, x2, y2, steps):
    """Convert SVG arc to polyline points."""
    if x1 == x2 and y1 == y2:
        return []
    if rx == 0 or ry == 0:
        return [(x1, y1), (x2, y2)]

    phi = math.radians(x_rot)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)

    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p =  cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy

    rx, ry = abs(rx), abs(ry)
    x1p2, y1p2, rx2, ry2 = x1p**2, y1p**2, rx**2, ry**2
    lam = x1p2/rx2 + y1p2/ry2
    if lam > 1:
        sq = math.sqrt(lam)
        rx, ry = sq * rx, sq * ry
        rx2, ry2 = rx**2, ry**2

    num = max(0, rx2*ry2 - rx2*y1p2 - ry2*x1p2)
    den = rx2*y1p2 + ry2*x1p2
    sq = math.sqrt(num / den) if den else 0
    if large == sweep:
        sq = -sq

    cxp =  sq * rx * y1p / ry
    cyp = -sq * ry * x1p / rx

    cx = cos_phi*cxp - sin_phi*cyp + (x1+x2)/2
    cy = sin_phi*cxp + cos_phi*cyp + (y1+y2)/2

    def angle(ux, uy, vx, vy):
        n = math.sqrt(ux**2+uy**2) * math.sqrt(vx**2+vy**2)
        if n == 0: return 0
        c = max(-1, min(1, (ux*vx+uy*vy)/n))
        a = math.acos(c)
        if ux*vy - uy*vx < 0: a = -a
        return a

    theta1 = angle(1, 0, (x1p-cxp)/rx, (y1p-cyp)/ry)
    dtheta = angle((x1p-cxp)/rx, (y1p-cyp)/ry, (-x1p-cxp)/rx, (-y1p-cyp)/ry)

    if not sweep and dtheta > 0: dtheta -= 2*math.pi
    if sweep and dtheta < 0: dtheta += 2*math.pi

    pts = []
    for i in range(steps + 1):
        t = i / steps
        theta = theta1 + t * dtheta
        x = cos_phi*rx*math.cos(theta) - sin_phi*ry*math.sin(theta) + cx
        y = sin_phi*rx*math.cos(theta) + cos_phi*ry*math.sin(theta) + cy
        pts.append((x, y))
    return pts


def _svg_parse_numbers(s: str) -> list[float]:
    return [float(x) for x in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', s)]


def _svg_path_to_polylines(d: str, steps: int = 16) -> list[list[tuple]]:
    """
    Parse SVG path 'd' attribute, return list of polylines.
    Each polyline is a list of (x, y) tuples.
    Beziers are sampled at 'steps' intervals.
    """
    tokens = re.findall(r'[MmZzLlHhVvCcSsQqTtAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d)
    cmd = None
    pos = (0.0, 0.0)
    start = (0.0, 0.0)
    last_ctrl = None
    polylines = []
    current = []

    def flush():
        nonlocal current
        if len(current) >= 2:
            polylines.append(current)
        current = []

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if re.match(r'[MmZzLlHhVvCcSsQqTtAa]', t):
            cmd = t
            i += 1
            if cmd in ('Z', 'z'):
                if current and current[-1] != start:
                    current.append(start)
                flush()
                pos = start
                cmd = None
            continue

        if cmd in ('Z', 'z'):
            if current and current[-1] != start:
                current.append(start)
            flush()
            pos = start
            cmd = None
            continue

        nparams = {
            'M':2,'m':2,'L':2,'l':2,'H':1,'h':1,'V':1,'v':1,
            'C':6,'c':6,'S':4,'s':4,'Q':4,'q':4,'T':2,'t':2,
            'A':7,'a':7,
        }.get(cmd, 0)

        if nparams == 0:
            i += 1
            continue

        args = []
        while len(args) < nparams and i < len(tokens):
            tok = tokens[i]
            if re.match(r'[MmZzLlHhVvCcSsQqTtAa]', tok):
                break
            args.append(float(tok))
            i += 1

        if len(args) < nparams:
            continue

        rel = cmd.islower()
        base = pos if rel else (0.0, 0.0)

        if cmd in ('M', 'm'):
            flush()
            nx, ny = base[0]+args[0], base[1]+args[1]
            pos = (nx, ny)
            start = pos
            current = [pos]
            last_ctrl = None
            cmd = 'l' if rel else 'L'

        elif cmd in ('L', 'l'):
            nx, ny = base[0]+args[0], base[1]+args[1]
            pos = (nx, ny)
            current.append(pos)
            last_ctrl = None

        elif cmd in ('H', 'h'):
            nx = (pos[0] if rel else 0) + args[0]
            pos = (nx, pos[1])
            current.append(pos)
            last_ctrl = None

        elif cmd in ('V', 'v'):
            ny = (pos[1] if rel else 0) + args[0]
            pos = (pos[0], ny)
            current.append(pos)
            last_ctrl = None

        elif cmd in ('C', 'c'):
            p1 = (base[0]+args[0], base[1]+args[1])
            p2 = (base[0]+args[2], base[1]+args[3])
            p3 = (base[0]+args[4], base[1]+args[5])
            for s in range(1, steps+1):
                current.append(_svg_cubic_point(pos, p1, p2, p3, s/steps))
            last_ctrl = p2
            pos = p3

        elif cmd in ('S', 's'):
            p1 = (2*pos[0]-last_ctrl[0], 2*pos[1]-last_ctrl[1]) if last_ctrl else pos
            p2 = (base[0]+args[0], base[1]+args[1])
            p3 = (base[0]+args[2], base[1]+args[3])
            for s in range(1, steps+1):
                current.append(_svg_cubic_point(pos, p1, p2, p3, s/steps))
            last_ctrl = p2
            pos = p3

        elif cmd in ('Q', 'q'):
            p1 = (base[0]+args[0], base[1]+args[1])
            p2 = (base[0]+args[2], base[1]+args[3])
            for s in range(1, steps+1):
                current.append(_svg_quadratic_point(pos, p1, p2, s/steps))
            last_ctrl = p1
            pos = p2

        elif cmd in ('T', 't'):
            p1 = (2*pos[0]-last_ctrl[0], 2*pos[1]-last_ctrl[1]) if last_ctrl else pos
            p2 = (base[0]+args[0], base[1]+args[1])
            for s in range(1, steps+1):
                current.append(_svg_quadratic_point(pos, p1, p2, s/steps))
            last_ctrl = p1
            pos = p2

        elif cmd in ('A', 'a'):
            rx,ry,xr = args[0],args[1],args[2]
            large,sweep = int(args[3]),int(args[4])
            ex,ey = base[0]+args[5], base[1]+args[6]
            pts = _svg_arc_to_lines(pos[0],pos[1],rx,ry,xr,large,sweep,ex,ey,steps)
            if pts:
                current.extend(pts[1:])
            last_ctrl = None
            pos = (ex, ey)

    flush()
    return polylines


SVG_COLORS = {
    "black":(0,0,0),"white":(1,1,1),"red":(1,0,0),"green":(0,0.502,0),
    "blue":(0,0,1),"yellow":(1,1,0),"cyan":(0,1,1),"magenta":(1,0,1),
    "gray":(0.502,0.502,0.502),"grey":(0.502,0.502,0.502),
    "none":None,
}

def parse_svg_color(s: str, opacity: float = 1.0):
    if not s or s == "none": return None
    s = s.strip().lower()
    if s in SVG_COLORS:
        c = SVG_COLORS[s]
        return [*c, opacity] if c else None
    if s.startswith("#"):
        h = s[1:]
        if len(h) in (3, 4): h = "".join(ch * 2 for ch in h)
        try:
            r,g,b = int(h[0:2],16)/255, int(h[2:4],16)/255, int(h[4:6],16)/255
            if len(h) == 8: opacity *= int(h[6:8],16)/255
        except ValueError:
            return [0, 0, 0, opacity]
        return [r, g, b, opacity]
    m = re.match(r'rgb\((\d+),\s*(\d+),\s*(\d+)\)', s)
    if m:
        return [int(m.group(i))/255 for i in (1,2,3)] + [opacity]
    try:
        # every CSS color name, hsl(), rgb(%) ...
        from PIL import ImageColor
        r, g, b = ImageColor.getrgb(s)[:3]
        return [r/255, g/255, b/255, opacity]
    except Exception:
        return [0, 0, 0, opacity]


# ── SVG style helpers (shared by the vector and bitmap SVG paths) ────────────
# Colors can come from attributes or inline `style`, and are inherited from
# parent groups. Not handled: <style> blocks/classes, gradients, transforms.

_UNSET = object()   # "this SVG never says what color to use"
_SVG_SHAPES = {"path", "line", "polyline", "polygon", "rect", "circle", "ellipse"}
_SVG_HIDDEN = {"defs", "clippath", "mask", "symbol", "pattern", "marker", "metadata",
               "title", "desc", "style", "script", "lineargradient", "radialgradient",
               "filter"}
_SVG_STYLE_KEYS = ("fill", "stroke", "stroke-width", "stroke-opacity",
                   "fill-opacity", "fill-rule", "color")


def _svg_tag(el) -> str:
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag


def _svg_style(el, inherited: dict) -> dict:
    """Computed style for `el`: inherited values, overridden by attributes,
    overridden by the inline style=\"...\" declarations."""
    props = {}
    for k in _SVG_STYLE_KEYS + ("opacity", "display"):
        v = el.get(k)
        if v is not None:
            props[k] = v.strip()
    for decl in (el.get("style") or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            props[k.strip().lower()] = v.strip()
    st = dict(inherited)
    for k in _SVG_STYLE_KEYS:
        if k in props and props[k].lower() != "inherit":
            st[k] = props[k]
    try:
        own_opacity = float(props.get("opacity", 1))
    except ValueError:
        own_opacity = 1.0
    st["_opacity"] = inherited.get("_opacity", 1.0) * own_opacity
    if props.get("display", "").lower() == "none":
        st["_hidden"] = True
    return st


def _svg_walk(el, inherited=None, skip_hidden=False):
    """Yield (element, tag, computed_style) for el and every descendant."""
    st = _svg_style(el, inherited or {"_opacity": 1.0})
    t = _svg_tag(el)
    if skip_hidden and (t.lower() in _SVG_HIDDEN or st.get("_hidden")):
        return
    yield el, t, st
    for child in el:
        yield from _svg_walk(child, st, skip_hidden)


def _svg_paint(st: dict, kind: str):
    """Resolve fill/stroke to [r,g,b,a], None (explicitly none), or _UNSET."""
    raw = st.get(kind)
    if raw is None:
        return _UNSET
    raw = raw.strip().lower()
    if raw in ("", "inherit") or raw.startswith("url("):
        return _UNSET
    if raw == "none":
        return None
    if raw == "currentcolor":
        raw = (st.get("color") or "").strip().lower()
        if not raw or raw == "currentcolor":
            return _UNSET
    try:
        opacity = float(st.get(kind + "-opacity", 1))
    except ValueError:
        opacity = 1.0
    c = parse_svg_color(raw, opacity * st.get("_opacity", 1.0))
    return c if c else None


def _svg_num(s, default: float = 0.0) -> float:
    m = re.match(r"\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)", s or "")
    return float(m.group(1)) if m else default


def _svg_dims(root) -> tuple[float, float, float, float]:
    """(x, y, width, height) of the SVG's drawing area in user units."""
    vb = root.get("viewBox")
    if vb:
        parts = _svg_parse_numbers(vb)
        if len(parts) == 4 and parts[2] > 0 and parts[3] > 0:
            return tuple(parts)
    w = _svg_num(root.get("width"), 100.0)
    h = _svg_num(root.get("height"), 100.0)
    return 0.0, 0.0, (w if w > 0 else 100.0), (h if h > 0 else 100.0)


def _svg_element_polylines(el, t: str, steps: int) -> list[list[tuple]]:
    """Flatten one basic shape into polylines of (x, y) in SVG user units."""
    g = lambda key, default=0.0: _svg_num(el.get(key), default)
    if t == "path":
        return _svg_path_to_polylines(el.get("d", ""), steps)
    if t == "line":
        return [[(g("x1"), g("y1")), (g("x2"), g("y2"))]]
    if t in ("polyline", "polygon"):
        raw = _svg_parse_numbers(el.get("points", ""))
        pts = [(raw[i], raw[i + 1]) for i in range(0, len(raw) - 1, 2)]
        if t == "polygon" and pts:
            pts.append(pts[0])
        return [pts]
    if t == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        return [[(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]]
    if t in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx, ry = (g("r", 1.0), g("r", 1.0)) if t == "circle" else (g("rx", 1.0), g("ry", 1.0))
        return [[(cx + rx * math.cos(2 * math.pi * i / steps),
                  cy + ry * math.sin(2 * math.pi * i / steps)) for i in range(steps + 1)]]
    return []


def svg_needs_fallback_color(svg_path, bitmap: bool) -> bool:
    """True if some shapes never specify a color (so the user should pick one)."""
    try:
        root = ET.parse(svg_path).getroot()
    except Exception:
        return False
    for _el, t, st in _svg_walk(root, skip_hidden=bitmap):
        if t not in _SVG_SHAPES:
            continue
        fill, stroke = _svg_paint(st, "fill"), _svg_paint(st, "stroke")
        if bitmap:
            if (stroke is _UNSET) if t == "line" else (fill is _UNSET):
                return True
        elif fill in (_UNSET, None) and stroke in (_UNSET, None):
            return True
    return False


def _composite_mask(canvas, mask, rgba):
    """Paint solid `rgba` through `mask` onto `canvas` (both full size)."""
    alpha = max(0.0, min(1.0, rgba[3] if len(rgba) > 3 else 1.0))
    if alpha < 1.0:
        mask = mask.point(lambda v: int(v * alpha))
    layer = Image.new("RGBA", canvas.size,
                      tuple(round(c * 255) for c in rgba[:3]) + (0,))
    layer.putalpha(mask)
    return Image.alpha_composite(canvas, layer)


def _xor_polygon(mask, poly):
    """Even-odd fill: toggle the area inside `poly` in `mask` (so holes work)."""
    from PIL import ImageChops, ImageDraw
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    x0, y0 = max(0, math.floor(min(xs))), max(0, math.floor(min(ys)))
    x1, y1 = min(mask.width, math.ceil(max(xs)) + 1), min(mask.height, math.ceil(max(ys)) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    layer = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(layer).polygon([(x - x0, y - y0) for x, y in poly], fill=255)
    box = (x0, y0, x1, y1)
    mask.paste(ImageChops.difference(mask.crop(box), layer), box)


def rasterize_svg(svg_path, scale: float = 1.0, smooth: bool = False, steps: int = 48,
                  color=None, fallback_color=None) -> tuple[bytes, int, int]:
    """Render an SVG to a filled RGBA PNG using its own fill/stroke colors.
    `color` overrides every color; `fallback_color` is used where the SVG gives
    none. With `smooth`, edges are anti-aliased (4x supersampling, bilinear
    downscale); without, they're hard pixels. Returns (png_bytes, w, h)."""
    from PIL import ImageDraw
    root = ET.parse(svg_path).getroot()
    vb_x, vb_y, vb_w, vb_h = _svg_dims(root)
    width, height = max(1, round(vb_w * scale)), max(1, round(vb_h * scale))
    if max(width, height) > 4096:
        raise ValueError(f"SVG would be {width}×{height}px; use a smaller scale")
    ss = 4 if smooth else 1
    while ss > 1 and width * height * ss * ss > 36_000_000:
        ss -= 1
    canvas = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    k = scale * ss
    fallback = fallback_color or [0, 0, 0, 1]

    for el, t, st in _svg_walk(root, skip_hidden=True):
        if t not in _SVG_SHAPES:
            continue
        polys = [[((x - vb_x) * k, (y - vb_y) * k) for x, y in poly]
                 for poly in _svg_element_polylines(el, t, steps) if len(poly) >= 2]
        if not polys:
            continue
        fill, stroke = _svg_paint(st, "fill"), _svg_paint(st, "stroke")
        if color:
            fill = None if fill is None else color
            stroke = None if stroke in (_UNSET, None) else color
        else:
            if fill is _UNSET:
                fill = None if t == "line" else fallback
            if stroke is _UNSET:
                stroke = fallback if t == "line" else None

        if fill is not None and t != "line":
            mask = Image.new("L", canvas.size, 0)
            for poly in polys:
                if len(poly) >= 3:
                    _xor_polygon(mask, poly)
            canvas = _composite_mask(canvas, mask, fill)
        if stroke is not None:
            line_w = max(1, round(_svg_num(st.get("stroke-width"), 1.0) * k))
            mask = Image.new("L", canvas.size, 0)
            draw = ImageDraw.Draw(mask)
            for poly in polys:
                draw.line(poly, fill=255, width=line_w, joint="curve")
            canvas = _composite_mask(canvas, mask, stroke)

    if ss > 1:
        canvas = canvas.resize((width, height), Image.BILINEAR)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), width, height


def svg_to_path_data(svg_path, steps: int = 16, scale: float = 1.0,
                     ppu: float = SVG_DEFAULT_PPU, color=None, fallback_color=None):
    """
    Parse an SVG file and return a Castle pathDataList and framesBounds.
    scale: multiplier applied to Castle units (1.0 = default)
    ppu: pixels per unit (default 25.6)
    color: override color [r,g,b,a], None = use SVG colors
    fallback_color: color for shapes the SVG doesn't give one (default black)
    """
    root = ET.parse(svg_path).getroot()

    # SVG Y is flipped vs Castle Y
    def to_castle_raw(x, y):
        return (x / ppu * scale, -(y / ppu) * scale)

    _cx_offset = [0.0]
    _cy_offset = [0.0]

    def to_castle(x, y):
        rx, ry = to_castle_raw(x, y)
        return (rx - _cx_offset[0], ry - _cy_offset[0])

    path_data = []
    default_color = fallback_color or [0, 0, 0, 1]
    all_xs, all_ys = [], []

    def get_color(st):
        if color: return color
        # Lines are drawn as outlines: prefer the stroke color, then the fill.
        for kind in ("stroke", "fill"):
            c = _svg_paint(st, kind)
            if c is not _UNSET and c is not None:
                return c
        return default_color

    for el, t, st in _svg_walk(root):
        c = get_color(st)
        for poly in _svg_element_polylines(el, t, steps):
            if len(poly) < 2: continue
            castle_pts = [to_castle(x, y) for x, y in poly]
            for j in range(len(castle_pts)-1):
                x1,y1 = castle_pts[j]
                x2,y2 = castle_pts[j+1]
                path_data.append({
                    "p": [round(x1,5), round(y1,5), round(x2,5), round(y2,5)],
                    "s": 1,
                    "f": False,
                    "c": [round(v,5) for v in c],
                })
                all_xs.extend([x1, x2])
                all_ys.extend([y1, y2])

    if not all_xs:
        return path_data, {"minX":-5,"maxX":5,"minY":-5,"maxY":5}, {"minX":-32,"maxX":32,"minY":-32,"maxY":32}

    mid_x = (min(all_xs) + max(all_xs)) / 2
    mid_y = (min(all_ys) + max(all_ys)) / 2
    for seg in path_data:
        seg["p"][0] = round(seg["p"][0] - mid_x, 5)
        seg["p"][1] = round(seg["p"][1] - mid_y, 5)
        seg["p"][2] = round(seg["p"][2] - mid_x, 5)
        seg["p"][3] = round(seg["p"][3] - mid_y, 5)
    all_xs = [x - mid_x for x in all_xs]
    all_ys = [y - mid_y for y in all_ys]

    bounds = {
        "minX": round(min(all_xs), 5),
        "maxX": round(max(all_xs), 5),
        "minY": round(min(all_ys), 5),
        "maxY": round(max(all_ys), 5),
    }

    fill_bounds = {
        "minX": round(min(all_xs) * ppu / scale),
        "maxX": round(max(all_xs) * ppu / scale),
        "minY": round(-max(all_ys) * ppu / scale),
        "maxY": round(-min(all_ys) * ppu / scale),
    }

    return path_data, bounds, fill_bounds


def build_drawing2_vector_frames(items, fps: float = 4, play_mode: str = "still",
                                 scale=10, ppu=SVG_DEFAULT_PPU) -> dict:
    """items: [(path_data, bounds, fill_bounds), ...] → one Drawing2 with one
    vector frame per item."""
    frames = [{
        "isLinked": False,
        "pathDataList": path_data,
        "fillImageBounds": fill_bounds,
        "avatarX": 0, "avatarY": 0, "avatarRadius": 5,
    } for path_data, _bounds, fill_bounds in items]
    hash_src = str(items[0][0]) if len(items) == 1 else str([it[0] for it in items])
    return {
        "initialFrame": 1, "currentFrame": 1,
        "framesPerSecond": fps,
        "playMode": play_mode,
        "loopStartFrame": -1, "loopEndFrame": -1,
        "opacity": 1,
        "hash": str(abs(hash(hash_src)))[:19],
        "playing": False, "loop": play_mode != "still",
        "drawData": {
            "color": [1,1,1,1], "lineColor": [0,0,0,1],
            "gridSize": 0.71428, "scale": scale, "version": 3,
            "fillPixelsPerUnit": ppu,
            "numTotalLayers": 1,
            "framesBounds": [it[1] for it in items],
            "colors": [], "selectedFrame": 1,
            "layers": [{
                "title": "Layer 1", "id": "layer1",
                "isVisible": True, "isBitmap": False, "isAvatar": False,
                "frames": frames,
            }],
        },
        "physicsBodyData": {
            "shapes": [{
                "p1": {"x": 5, "y": 5}, "p2": {"x": -5, "y": -5},
                "p3": {"x": 0, "y": 0}, "radius": 0, "x": 0, "y": 0,
                "type": "rectangle",
            }],
            "scale": scale, "version": 2, "zeroShapesInV1": False,
        },
        "disabled": False,
    }


def build_drawing2_vector(path_data, bounds, fill_bounds,
                           scale=10, ppu=SVG_DEFAULT_PPU) -> dict:
    return build_drawing2_vector_frames([(path_data, bounds, fill_bounds)],
                                        scale=scale, ppu=ppu)


# ── font → Drawing2 (one actor, one frame per glyph) ─────────────────────────
# Every glyph is rendered in the SAME font-space coordinate system (baseline
# at y=0, each glyph's own natural left-side-bearing at x=0) rather than
# being individually re-centered on its own bounding box like svg_to_path_data
# does for standalone images. That keeps every frame anchored to one shared
# pen origin, so switching which frame is showing doesn't jump the glyph
# around -- it just swaps the shape, the way a real font's glyphs share one
# baseline.

# Named unicode ranges a user can pick from. Each entry is either an
# (start, end) inclusive codepoint range, or a callable returning a list of
# codepoints for sets that aren't one contiguous range.
def _charset_standard():
    # 0-255 excluding the C0 controls, DEL, and the C1 controls.
    return [cp for cp in range(0x00, 0x100)
            if not (cp < 0x20 or cp == 0x7F or 0x80 <= cp <= 0x9F)]

def _charset_alphanumeric():
    return list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x61, 0x7B))

FONT_CHARSETS = {
    "Standard (0-255, no control chars)": _charset_standard,
    "ASCII": lambda: list(range(0x20, 0x7F)),
    "Alphanumeric": _charset_alphanumeric,
    "Numeric": lambda: list(range(0x0030, 0x003A)),
    "Currency Symbols": lambda: list(range(0x20A0, 0x20D0)),
    "Greek Characters": lambda: list(range(0x0391, 0x03CA)),
    "Fraction Symbols": lambda: list(range(0x2150, 0x2160)),
    "Arrows": lambda: list(range(0x2190, 0x2200)),
    "Mathematical Operators": lambda: list(range(0x2200, 0x2300)),
    "Miscellaneous Technical": lambda: list(range(0x2300, 0x2400)),
    "Box Drawing": lambda: list(range(0x2500, 0x2580)),
    "Block Elements": lambda: list(range(0x2580, 0x25A0)),
    "Dingbats": lambda: list(range(0x2700, 0x27C0)),
    "Braille Patterns": lambda: list(range(0x2800, 0x2900)),
    "Musical Symbols": lambda: list(range(0x1D100, 0x1D200)),
}

def font_glyph_svg_d(glyph_set, glyph_name: str) -> str:
    """
    SVG-style path 'd' string for one glyph, in the font's own native
    coordinate space (Y-up, baseline at 0). No pre-flip here -- the single
    Y-negation in font_glyph_to_path_data's to-Castle conversion is what
    orients it correctly; adding a second flip here would cancel that out
    and undo it.
    """
    svg_pen = SVGPathPen(glyph_set)
    glyph_set[glyph_name].draw(svg_pen)
    return svg_pen.getCommands()

def font_glyph_to_path_data(d: str, steps: int, ppu: float, scale: float,
                             color: list) -> tuple[list[dict], dict]:
    """
    Convert one glyph's Y-flipped path 'd' string into Castle pathDataList
    segments, in the SHARED font-space coordinate system (no per-glyph
    recentring -- see module note above).
    """
    polylines = _svg_path_to_polylines(d, steps)
    path_data = []
    all_xs, all_ys = [], []
    for poly in polylines:
        if len(poly) < 2:
            continue
        castle_pts = [(x / ppu * scale, -(y / ppu) * scale) for x, y in poly]
        for j in range(len(castle_pts) - 1):
            x1, y1 = castle_pts[j]
            x2, y2 = castle_pts[j + 1]
            path_data.append({
                "p": [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)],
                "s": 1, "f": False,
                "c": [round(v, 5) for v in color],
            })
            all_xs.extend([x1, x2])
            all_ys.extend([y1, y2])
    if not all_xs:
        bounds = {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0}
    else:
        bounds = {
            "minX": round(min(all_xs), 5), "maxX": round(max(all_xs), 5),
            "minY": round(min(all_ys), 5), "maxY": round(max(all_ys), 5),
        }
    return path_data, bounds

def build_drawing2_font(glyph_frames: list[tuple[int, list, dict]],
                         ppu: float, scale: float = 10) -> dict:
    """
    glyph_frames: [(codepoint, path_data, bounds), ...] in the order they
    should appear as frames 1, 2, 3, ...
    """
    castle_frames, frame_bounds = [], []
    for _cp, path_data, bounds in glyph_frames:
        castle_frames.append({
            "isLinked": False,
            "pathDataList": path_data,
            "fillImageBounds": bounds,
            "avatarX": 0, "avatarY": 0, "avatarRadius": 5,
        })
        frame_bounds.append(bounds)

    overall = {
        "minX": min((b["minX"] for b in frame_bounds), default=-5),
        "maxX": max((b["maxX"] for b in frame_bounds), default=5),
        "minY": min((b["minY"] for b in frame_bounds), default=-5),
        "maxY": max((b["maxY"] for b in frame_bounds), default=5),
    }

    return {
        "initialFrame": 1, "currentFrame": 1,
        "framesPerSecond": 4,
        "playMode": "still",
        "loopStartFrame": -1, "loopEndFrame": -1,
        "opacity": 1,
        "hash": str(abs(hash(str(glyph_frames))))[:19],
        "playing": False, "loop": False,
        "drawData": {
            "color": [1, 1, 1, 1], "lineColor": [0, 0, 0, 1],
            "gridSize": 0.71428, "scale": scale, "version": 3,
            "fillPixelsPerUnit": ppu,
            "numTotalLayers": 1,
            "framesBounds": frame_bounds,
            "colors": [], "selectedFrame": 1,
            "layers": [{
                "title": "Layer 1", "id": "layer1",
                "isVisible": True, "isBitmap": False, "isAvatar": False,
                "frames": castle_frames,
            }],
        },
        "physicsBodyData": {
            "shapes": [{
                "p1": {"x": overall["maxX"], "y": overall["maxY"]},
                "p2": {"x": overall["minX"], "y": overall["minY"]},
                "p3": {"x": 0, "y": 0}, "radius": 0, "x": 0, "y": 0,
                "type": "rectangle",
            }],
            "scale": scale, "version": 2, "zeroShapesInV1": False,
        },
        "disabled": False,
    }


# ── midi → Music ─────────────────────────────────────────────────────────────

def beat_key(b): return f"{b:.6f}"
def make_color(): return {"r": 0.19607, "g": 0.16862, "b": 0.15681, "a": 1.0}

DEFAULT_TEMPO = 500000  # microseconds per beat == 120 BPM, mido/MIDI spec default

def get_tempo_map(mid) -> list[tuple[int, int]]:
    """
    Scan every track for `set_tempo` meta messages and return a sorted,
    deduped list of (abs_tick, tempo_us_per_beat) covering the whole file.
    Tempo events can technically appear on any track, not just track 0,
    so all tracks are scanned and merged by absolute tick.
    """
    events = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "set_tempo":
                events.append((abs_tick, msg.tempo))
    if not events:
        return [(0, DEFAULT_TEMPO)]
    events.sort(key=lambda e: e[0])
    # if multiple tempo changes land on the same tick, keep the last one
    deduped = []
    for tick, tempo in events:
        if deduped and deduped[-1][0] == tick:
            deduped[-1] = (tick, tempo)
        else:
            deduped.append((tick, tempo))
    if deduped[0][0] != 0:
        deduped.insert(0, (0, DEFAULT_TEMPO))
    return deduped

def make_tick_scaler(tempo_map: list[tuple[int, int]]):
    """
    Build a function mapping abs_tick -> "scaled tick", where the scaling
    compresses/expands ticks by how much each tempo segment's speed differs
    from the file's reference (initial) tempo. Castle plays music at a
    single constant BPM, so a MIDI's tempo *changes* have to be baked into
    the beat spacing itself, otherwise sections that sped up or slowed down
    in the original file play back at the wrong relative speed in Castle.
    A file with only one tempo (the common case) scales by exactly 1.0
    everywhere, so this is a no-op for tempo-stable MIDIs.
    """
    reference_tempo = tempo_map[0][1]
    ticks = [t for t, _ in tempo_map]
    # cumulative scaled-tick value at the start of each tempo segment.
    # A segment's real duration is delta_ticks * tempo (µs/beat); dividing
    # that real duration back through reference_tempo gives how many ticks
    # it "would have been" at the reference tempo, so faster segments
    # (smaller µs/beat) compress and slower segments expand.
    cum_at_start = [0.0]
    for i in range(1, len(tempo_map)):
        prev_tick, prev_tempo = tempo_map[i - 1]
        cur_tick, _ = tempo_map[i]
        delta_ticks = cur_tick - prev_tick
        cum_at_start.append(cum_at_start[-1] + delta_ticks * (prev_tempo / reference_tempo))

    def scale(abs_tick: int) -> float:
        idx = bisect.bisect_right(ticks, abs_tick) - 1
        idx = max(0, idx)
        seg_tick, seg_tempo = tempo_map[idx]
        return cum_at_start[idx] + (abs_tick - seg_tick) * (seg_tempo / reference_tempo)

    return scale

def collect_midi_tracks(mid) -> list[dict]:
    """
    Returns one entry per non-empty MIDI track:
    {"events": [(beat, note, velocity), ...], "program": int, "is_drum": bool}

    Since a Castle track only has one instrument, each MIDI track is
    reduced to a single representative (channel, program) pair: whichever
    combination its note_on events use most. Channel 10 (index 9) is
    GM's reserved percussion channel regardless of its program number.
    """
    tpb = mid.ticks_per_beat
    scale = make_tick_scaler(get_tempo_map(mid))
    result = []
    for track in mid.tracks:
        events, abs_tick = [], 0
        programs = {}       # channel -> most recently seen program
        usage = Counter()   # (channel, program) -> note_on count
        for msg in track:
            abs_tick += msg.time
            if msg.type == "program_change":
                programs[msg.channel] = msg.program
            elif msg.type == "note_on" and msg.velocity > 0:
                events.append((scale(abs_tick) / tpb * 4, msg.note, msg.velocity))
                channel = msg.channel
                usage[(channel, programs.get(channel, 0))] += 1
        if not events:
            continue
        (channel, program), _ = usage.most_common(1)[0]
        result.append({
            "events": events,
            "program": program,
            "is_drum": channel == 9,
        })
    return result

def get_bpm(mid) -> float:
    """BPM implied by the MIDI file's initial (reference) tempo."""
    tempo_us = get_tempo_map(mid)[0][1]
    return 60_000_000 / tempo_us

# GM instrument families, in program-number order (0-indexed, inclusive
# upper bound), each mapped to one of Castle's 4 tone waveforms plus an
# attack/release approximating that family's envelope. Castle only offers
# square/sawtooth/sine/noise, so this groups all 128 GM programs into
# their 16 official families rather than mapping note-for-note.
GM_FAMILIES = [
    (7,   "sine",     0.0,  0.35),  # Piano
    (15,  "sine",     0.0,  0.2),   # Chromatic Percussion
    (23,  "square",   0.0,  0.1),   # Organ
    (31,  "sawtooth", 0.0,  0.3),   # Guitar
    (39,  "square",   0.0,  0.3),   # Bass
    (47,  "sawtooth", 0.08, 0.45),  # Strings
    (55,  "sawtooth", 0.1,  0.45),  # Ensemble
    (63,  "square",   0.02, 0.25),  # Brass
    (71,  "square",   0.02, 0.2),   # Reed
    (79,  "sine",     0.03, 0.2),   # Pipe
    (87,  "sawtooth", 0.0,  0.15),  # Synth Lead
    (95,  "sine",     0.3,  0.6),   # Synth Pad
    (103, "sine",     0.15, 0.4),   # Synth Effects
    (111, "sawtooth", 0.0,  0.3),   # Ethnic
    (119, "noise",    0.0,  0.1),   # Percussive
    (127, "noise",    0.0,  0.2),   # Sound Effects
]

def gm_program_to_tone(program: int, is_drum: bool) -> tuple[str, float, float]:
    """Map a GM program (+ drum-channel flag) to (waveform, attack, release)."""
    if is_drum:
        return "noise", 0.0, 0.12
    program = max(0, min(127, program))
    for upper, waveform, attack, release in GM_FAMILIES:
        if program <= upper:
            return waveform, attack, release
    return "sawtooth", 0.0, 0.4  # unreachable, GM_FAMILIES covers 0-127

def build_music(mid, beats_per_bar: int = 4) -> dict:
    midi_tracks = collect_midi_tracks(mid)
    patterns, castle_tracks = {}, []
    for t_idx, track in enumerate(midi_tracks):
        waveform, attack, release = gm_program_to_tone(track["program"], track["is_drum"])

        # merge every note in the track into a single pattern, keyed by
        # its absolute beat position (instead of one pattern per bar).
        notes_by_beat = {}
        for beat, note, velocity in track["events"]:
            # "key" already sits at the same octave the MIDI file uses
            # (raw note number), so bump it one octave higher per spec.
            octave_note = min(127, note + 12)
            notes_by_beat.setdefault(round(beat, 9), []).append((octave_note, velocity))

        pid = str(uuid.uuid4())
        notes = {beat_key(b): [{"key": n, "vel": v} for n, v in ns]
                 for b, ns in sorted(notes_by_beat.items())}
        patterns[pid] = {
            "patternId": pid,
            "name": f"t{t_idx}",
            "color": make_color(), "loop": "nextBar", "loopLength": 0,
            "notes": notes,
        }
        castle_tracks.append({
            "instrument": {
                "type": "sampler",
                "props": {"name": "tone", "muted": False, "volume": 1},
                "sample": {
                    "type": "tone",
                    "playbackRate": {"value": 1}, "amplitude": {"value": 1},
                    "pan": {"value": 0}, "recordingUrl": "", "uploadUrl": "",
                    "category": "random", "seed": 1337, "mutationSeed": 0,
                    "mutationAmount": 5, "midiNote": 48, "waveform": waveform,
                    "attack": attack, "release": release, "wait": False,
                },
            },
            "sequence": {
                beat_key(0): {"patternId": pid, "loop": False}
            },
        })
    return {
        "song": {"patterns": patterns, "tracks": castle_tracks},
        "autoplay": "loop", "disabled": False,
    }

# ── main flow ─────────────────────────────────────────────────────────────────

# The CLI-scaffolded placeholder script Castle generates for a brand-new
# actor. If a blueprint still has exactly this (untouched) when an image
# is added to it, the placeholder is deleted since it's no longer needed.
AUTO_GENERATED_SCRIPT = """function onCreate()
  print("ready")
end

function onDraw()
  castle.draw.setColor(0.25, 0.9, 0.62, 1)
  castle.draw.circle("fill", 0, 0, 0.28)

  castle.draw.setLineWidth(0.035)
  castle.draw.setColor(1, 1, 1, 0.45)
  castle.draw.rectangle("line", -0.42, -0.42, 0.84, 0.84)
end"""

def maybe_delete_placeholder_script(card: Path, bp_path: Path):
    """
    If the blueprint's matching script (<card>/scripts/<blueprint-name>.lua)
    is exactly the CLI's auto-generated placeholder, clear its contents,
    since an image was just added to that actor and it needs no default
    script. The file itself is kept (rather than deleted) since Castle
    re-creates it with the placeholder code if it's missing.
    """
    script_path = card / "scripts" / f"{bp_path.stem}.lua"
    if not script_path.exists():
        return
    try:
        content = script_path.read_text(encoding="utf-8")
    except OSError:
        return
    if content.replace("\r\n", "\n").rstrip("\n") == AUTO_GENERATED_SCRIPT.rstrip("\n"):
        script_path.write_text("", encoding="utf-8")
        ps(f"Cleared placeholder script: {script_path.name}")

# ── supported file types / archives ──────────────────────────────────────────
# Every file castletool accepts is sorted into one "purpose" category. A
# compressed archive may only contain files of ONE category.

ISSUES_URL = "https://github.com/MGoosePlayZ/castletool/issues"

IMAGE_EXTS  = {".png", ".apng", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff",
               ".ico", ".heic", ".heif", ".gif", ".webp"}
VIDEO_EXTS  = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".flv", ".m4v",
               ".3gp", ".ogv"}
VECTOR_EXTS = {".svg"}
AUDIO_EXTS  = {".mp3", ".wav", ".ogg", ".oga", ".m4a", ".flac", ".aac"}
FONT_EXTS   = {".ttf", ".otf", ".woff", ".eot"}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2",
                    ".tar.xz", ".txz")
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3   # refuse to unpack more than 2 GiB

CATEGORY_LABELS = {
    "image": "image", "animation": "animated image", "video": "video",
    "vector": "vector graphic", "audio": "audio", "font": "font",
}
ACTION_FOR_CATEGORY = {
    "image": "Add image", "animation": "Add image", "video": "Add image",
    "vector": "Add image", "audio": "Add audio", "font": "Add Font",
}


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def classify_file(path: Path) -> str | None:
    """Return the file's purpose category, or None if it isn't supported."""
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:  return "video"
    if ext in VECTOR_EXTS: return "vector"
    if ext in AUDIO_EXTS:  return "audio"
    if ext in FONT_EXTS:   return "font"
    if ext in IMAGE_EXTS:
        # GIF/WEBP/PNG may or may not be animated; ask Pillow.
        if ext in (".png", ".apng", ".gif", ".webp") and HAS_PIL:
            try:
                with Image.open(path) as im:
                    if getattr(im, "is_animated", False):
                        return "animation"
            except Exception:
                pass
        return "image"
    return None


def report_unsupported(path: Path):
    ext = path.suffix.lower() or "(no extension)"
    pe(f"Unsupported file type: {ext}")
    p(f"If you'd like {ext} supported, please make an issue on GitHub: {ISSUES_URL}")


def check_category(cat: str, expected_action: str) -> bool:
    """True if `cat` belongs to `expected_action`; otherwise explain where it goes."""
    right = ACTION_FOR_CATEGORY[cat]
    if right == expected_action:
        return True
    label = CATEGORY_LABELS[cat]
    article = "an" if label[0] in "aeiou" else "a"
    pe(f"That's {article} {label} file. Use \"{right}\" for it, not \"{expected_action}\".")
    return False


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _safe_dest(root: Path, member_name: str) -> Path | None:
    """Map an archive member name to a path inside `root`, or None to skip it
    (path traversal, macOS resource-fork junk, etc)."""
    parts = [x for x in member_name.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if any(x == "__MACOSX" or x == ".DS_Store" or x.startswith("._") for x in parts):
        return None
    target = root.joinpath(*parts)
    try:
        target.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return target


def extract_archive(path: Path, dest: Path) -> list[Path]:
    """Unpack a .zip / .tar(.gz/.bz2/.xz) into `dest` (subdirectories included)
    and return every extracted file in natural path order."""
    written: list[Path] = []
    total = 0

    def _check(size: int):
        nonlocal total
        total += size
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("archive is too large to unpack (limit 2 GiB)")

    if path.name.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _safe_dest(dest, info.filename)
                if target is None:
                    continue
                _check(info.file_size)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(target)
    else:
        with tarfile.open(path) as tf:
            for member in tf:
                if not member.isreg():
                    continue
                target = _safe_dest(dest, member.name)
                if target is None:
                    continue
                _check(member.size)
                src = tf.extractfile(member)
                if src is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(target)

    written.sort(key=lambda f: _natural_key(str(f.relative_to(dest))))
    return written


def gather_media(path: Path, tmp_dir: Path):
    """Turn what the user pointed at (a file or an archive) into
    (files, category). Prints the reason and returns None if nothing usable."""
    if not is_archive(path):
        if path.stat().st_size == 0:
            pe(f"{path.name} is empty (0 bytes).")
            return None
        cat = classify_file(path)
        if cat is None:
            report_unsupported(path)
            return None
        return [path], cat

    pb(f"Reading archive: {path.name}")
    try:
        files = extract_archive(path, tmp_dir)
    except Exception as e:
        pe(f"Could not read archive: {e}")
        return None
    if not files:
        pe("The archive doesn't contain any files.")
        return None

    by_cat: dict[str, list[Path]] = {}
    ignored = 0
    empty = 0
    for f in files:
        if f.stat().st_size == 0:   # some icon sets ship empty placeholder files
            empty += 1
            continue
        cat = classify_file(f)
        if cat is None:
            ignored += 1
        else:
            by_cat.setdefault(cat, []).append(f)
    if empty:
        pw(f"Ignored {empty} empty file(s) in the archive.")
    if ignored:
        pw(f"Ignored {ignored} unsupported file(s) in the archive.")
    if not by_cat:
        pe("No supported files were found in the archive.")
        p(f"If you'd like a format supported, please make an issue on GitHub: {ISSUES_URL}")
        return None
    if len(by_cat) > 1:
        pe("Supported files are of different purpose")
        found = ", ".join(f"{len(v)} {CATEGORY_LABELS[c]}" for c, v in by_cat.items())
        pw(f"Found: {found}")
        return None
    cat, flist = next(iter(by_cat.items()))
    ps(f"Found {len(flist)} {CATEGORY_LABELS[cat]} file(s).")
    return flist, cat


# ── HEIC/HEIF (optional plugin) ──────────────────────────────────────────────

def ensure_heif() -> bool:
    """Make sure Pillow can open HEIC/HEIF. Tries to pip-install pillow-heif
    on first use (it's optional because it can fail to build on some devices)."""
    global HAS_HEIF
    if HAS_HEIF:
        return True

    def _try() -> bool:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            return True
        except Exception:
            return False

    HAS_HEIF = _try()
    if not HAS_HEIF:
        pb("Installing pillow-heif...")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "pillow-heif"])
        HAS_HEIF = _try()
    if not HAS_HEIF:
        pe("HEIC/HEIF support needs the 'pillow-heif' package, and it couldn't be installed. "
           "Try: pip install pillow-heif")
    return HAS_HEIF


def usable_files(files: list[Path]) -> list[Path]:
    """Drop files whose decoder isn't available (currently just HEIC/HEIF)."""
    if any(f.suffix.lower() in (".heic", ".heif") for f in files) and not ensure_heif():
        files = [f for f in files if f.suffix.lower() not in (".heic", ".heif")]
        if not files:
            pe("Nothing left to import.")
    return files


# ── ICO resolution picking ───────────────────────────────────────────────────

def ico_sizes_of(path: Path) -> list[tuple[int, int]]:
    """Resolutions embedded in an .ico, largest first."""
    with Image.open(path) as im:
        sizes = im.info.get("sizes")
        if not sizes and hasattr(im, "ico"):
            sizes = im.ico.sizes()
        if not sizes:
            sizes = {im.size}
    return sorted(sizes, key=lambda s: (s[0] * s[1], s[0]), reverse=True)


def pick_ico_sizes(files: list[Path]) -> dict[Path, tuple[int, int]]:
    """Let the user choose which embedded resolution to use for each .ico."""
    icos = [f for f in files if f.suffix.lower() == ".ico"]
    result: dict[Path, tuple[int, int]] = {}
    policy = None
    for f in icos:
        try:
            sizes = ico_sizes_of(f)
        except Exception as e:
            pe(f"Could not read {f.name}: {e}")
            continue
        if len(sizes) == 1:
            result[f] = sizes[0]
            continue
        if len(icos) > 1 and policy is None:
            policy = choose("Several .ico files have multiple resolutions. How should they be picked?", [
                "Largest available for every icon",
                "Smallest available for every icon",
                "Let me choose for each icon",
            ])
        if policy and policy.startswith("Largest"):
            result[f] = sizes[0]
        elif policy and policy.startswith("Smallest"):
            result[f] = sizes[-1]
        else:
            labels = [f"{w}×{h}" for w, h in sizes]
            chosen = choose(f"Select a resolution for {f.name}:", labels)
            result[f] = sizes[labels.index(chosen)]
    return result


# ── forking extra actors ─────────────────────────────────────────────────────

def strip_audio_rules(actor: dict):
    rules = actor.get("actorBlueprint", {}).get("components", {}).get("Rules")
    if rules and isinstance(rules.get("rules"), list):
        rules["rules"] = [r for r in rules["rules"] if not is_audio_loop_rule(r)]


def fork_actor(card: Path, base_actor: dict, name: str) -> tuple[Path, dict]:
    """Copy `base_actor` into a brand-new blueprint file in the card's
    blueprints folder (new id, titled after `name`). Returns (path, actor)."""
    bp_dir = card / "scene" / "blueprints"
    bp_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "actor"
    path = bp_dir / f"{stem}.json"
    n = 2
    while path.exists():
        path = bp_dir / f"{stem}_{n}.json"
        n += 1
    new = copy.deepcopy(base_actor)
    if "entryId" in new:
        new["entryId"] = str(uuid.uuid4())
    new["title"] = name
    if isinstance(new.get("library"), dict):
        new["library"]["blueprintAssetId"] = str(uuid.uuid4())
    return path, new


def write_actor(path: Path, actor: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(actor, f, indent=2)


def next_target(card: Path, bp_path: Path, actor: dict, base: dict,
                used_selected: bool, name: str):
    """The first successful file goes into the selected actor; each one after
    that goes into a fresh fork of the actor as it was before this action.
    Returns (path, actor, is_selected)."""
    if not used_selected:
        return bp_path, actor, True
    path, new = fork_actor(card, base, name)
    return path, new, False


# ── shared audio upload (used by video-with-sound and plain audio files) ─────

def attach_audio(actor: dict, src: Path, is_video: bool) -> bool:
    """Upload `src`'s sound to Castle and add a play-on-create loop rule to
    `actor`. Castle limits sounds to 30 seconds, so longer audio is split into
    parts under 30s that are uploaded one by one and chained to play in order.
    Videos have their track extracted; MP3s that fit are sent as-is.
    Returns True on success (the actor is left untouched on failure)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="castletool-audio-"))
    try:
        pb("Extracting audio..." if is_video else f"Converting {src.name}...")
        parts = split_audio_mp3(src, tmp_dir, is_video)
        urls = []
        for i, part in enumerate(parts, 1):
            label = f" (part {i}/{len(parts)})" if len(parts) > 1 else ""
            pb(f"Uploading audio to Castle{label}...")
            urls.append(upload_audio_file(part)["url"])
        add_or_replace_audio_rule(actor, urls)
        if len(urls) > 1:
            ps(f"Audio added: {len(urls)} parts (each under {AUDIO_CHUNK_SECONDS}s), played back to back")
        else:
            ps(f"Audio added: {urls[0]}")
        return True
    except Exception as e:
        pe(f"Audio upload failed: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── image / animation / video / vector → Drawing2 ────────────────────────────

SMOOTH_PROMPT = ("Enable bilinear interpolation? "
                 "(smooth scaling/edges instead of crisp pixels)")


def _ask_scale(prompt: str, default: str = "1") -> float:
    raw = ask(prompt, default=default)
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        pw("Invalid scale, using 1.")
        return 1.0


def _ask_size_handling() -> bool:
    """Still-image/bitmap-SVG archives: True = "don't scale" (1 pixel = 1 pixel)."""
    choice = choose("How should image sizes be handled?", [
        "Scale images to fit (keeps all images the same size)",
        "Don't scale images (1 pixel = 1 pixel, best for pixel art)",
    ])
    return choice.startswith("Don't")


def prompt_visual_settings(cat: str, files: list[Path], multi: bool,
                           frames_mode: bool = False) -> dict:
    """Ask the processing questions once; they apply to every file.
    frames_mode: the files become frames of one actor (so they get resized to
    match unless "don't scale" is chosen, which keeps interpolation relevant)."""
    no_scale = False
    st = {"scale": 1.0, "every": 1, "quantize": 0,
          "svg_scale": 1.0, "steps": 16, "color": None,
          "smooth": False, "bitmap": False, "fallback_color": None,
          "no_scale": False, "fixed_scale": None}
    note = " (applies to every file)" if multi else ""
    p()

    if cat == "vector":
        style = choose("How should the SVG be drawn?" + note, [
            "Vector (line segments)",
            "Bitmap (filled in, keeps its colors)",
        ])
        st["bitmap"] = style.startswith("Bitmap")
        if st["bitmap"]:
            if multi:
                no_scale = _ask_size_handling()
            st["scale"] = _ask_scale("Scale (1 = the SVG's own size in pixels, 2 = double)")
            st["smooth"] = yn(SMOOTH_PROMPT, default="n")
        else:
            if yn("Would you like to scale the SVG output?" + note, default="n"):
                while True:
                    raw = ask("Enter scale multiplier (e.g. 2.0 = twice as large)", default="1.0")
                    try:
                        st["svg_scale"] = float(raw)
                        break
                    except ValueError:
                        pe("Enter a number like 1.0 or 0.5")
            if yn("Customize bezier curve smoothness? (default 16 steps)", default="n"):
                while True:
                    raw = ask("Steps per curve segment", default="16")
                    if raw.isdigit() and int(raw) >= 2:
                        st["steps"] = int(raw)
                        break
                    pe("Enter a number ≥ 2")
        # Use the SVG's own colors; only ask where it doesn't say.
        if yn("Override the SVG's own colors with a single custom color?", default="n"):
            st["color"] = ask_color("Enter override color")
        elif any(svg_needs_fallback_color(f, st["bitmap"]) for f in files):
            p("Some shapes in the SVG don't specify a color.")
            st["fallback_color"] = ask_color("Pick a color for them")
        if not st["bitmap"]:
            return st
    else:
        if cat == "image" and multi:
            no_scale = _ask_size_handling()
        if no_scale:
            p("Images keep their original size (1 pixel = 1 pixel).")
        else:
            st["scale"] = _ask_scale("Scale (1 = full resolution, 0.5 = half, 2 = double)" + note)
            if st["scale"] != 1.0 or frames_mode:
                st["smooth"] = yn(SMOOTH_PROMPT, default="n")
    st["no_scale"] = no_scale

    if cat in ("animation", "video"):
        p()
        if yn("Would you like to skip frames? (reduces file size for long GIFs)", default="n"):
            while True:
                raw = ask("Keep every Nth frame (e.g. 2 = half frames, 4 = quarter)", default="2")
                if raw.isdigit() and int(raw) >= 1:
                    st["every"] = int(raw)
                    break
                pe("Enter a positive integer.")

    p()
    file_size = max(f.stat().st_size for f in files)
    if file_size > 500_000:
        pw(f"This file is abnormally large ({file_size//1024}KB). Quantizing is recommended.")
    if yn("Would you like to quantize this image? (reduces file size)" if not multi
          else "Would you like to quantize these images? (reduces file size)",
          default="y" if file_size > 500_000 else "n"):
        while True:
            raw = ask("Select number of colors", default="256")
            if raw.isdigit() and 1 <= int(raw) <= 256:
                st["quantize"] = int(raw)
                break
            pe("Enter a number between 1 and 256.")
    return st


def visual_native_size(path: Path, cat: str, ico_size=None) -> tuple[int, int]:
    if cat == "video":
        return probe_video_dimensions(path)
    if ico_size:
        return ico_size
    with Image.open(path) as im:
        return im.size


def scaled_size(native: tuple[int, int], scale: float) -> tuple[int, int]:
    return max(1, round(native[0] * scale)), max(1, round(native[1] * scale))


def fixed_density_scale(files: list[Path], ico_sizes: dict,
                        cat: str = "image", svg_scale: float = 1.0) -> int | None:
    """One pixels-per-unit for a whole batch, sized so the LARGEST image looks
    as it normally would. Every image then shares the same pixel size.
    (Bitmap SVGs: their rasterized size at `svg_scale`.)"""
    dims = []
    for f in files:
        try:
            if cat == "vector":
                _x, _y, vw, vh = _svg_dims(ET.parse(f).getroot())
                dims.append(scaled_size((vw, vh), svg_scale))
            else:
                dims.append(visual_native_size(f, "image", ico_sizes.get(f)))
        except Exception:
            pass
    if not dims:
        return None
    return max(1, round(max(max(d) for d in dims) / 20))


def render_visual(path: Path, cat: str, st: dict, ico_size=None):
    """Convert one file into a Drawing2 component. Returns (drawing2, summary)."""
    if cat == "vector" and st["bitmap"]:
        pb(f"Rasterizing SVG: {path.name}")
        png, width, height = rasterize_svg(
            path, scale=st["scale"], smooth=st["smooth"],
            color=st["color"], fallback_color=st["fallback_color"])
        frames = [png]
        if st["quantize"]:
            pb(f"Quantizing to {st['quantize']} colors...")
            frames = [quantize_frame(f, st["quantize"]) for f in frames]
        drawing2 = build_drawing2(frames, 1.0, width, height, "still",
                                  scale=st["fixed_scale"])
        return drawing2, f"SVG ready: bitmap, {width}×{height}px"

    if cat == "vector":
        pb(f"Loading SVG: {path.name}")
        path_data, bounds, fill_bounds = svg_to_path_data(
            path, steps=st["steps"], scale=st["svg_scale"], color=st["color"],
            fallback_color=st["fallback_color"])
        drawing2 = build_drawing2_vector(path_data, bounds, fill_bounds)
        return drawing2, f"SVG ready: {len(path_data)} line segments"

    width, height = scaled_size(visual_native_size(path, cat, ico_size), st["scale"])
    pb(f"Loading {'video' if cat == 'video' else 'image'}: {path.name}")
    if cat == "video":
        frames, fps = extract_mp4_frames(path, width, height, every_n=st["every"],
                                         smooth=st["smooth"])
    else:
        frames, fps = load_image_frames(path, width, height, ico_size=ico_size,
                                        smooth=st["smooth"])
        if st["every"] > 1:
            frames = frames[::st["every"]]
            fps = fps / st["every"]
    if st["quantize"]:
        pb(f"Quantizing {len(frames)} frame(s) to {st['quantize']} colors...")
        frames = [quantize_frame(f, st["quantize"]) for f in frames]
    play_mode = "loop" if (cat in ("animation", "video") or len(frames) > 1) else "still"
    drawing2 = build_drawing2(frames, fps, width, height, play_mode,
                              scale=st["fixed_scale"])
    return drawing2, f"Image ready: {len(frames)} frame(s), {fps} FPS, {width}×{height}px"


def render_frames_actor(files: list[Path], st: dict, ico_sizes: dict, fps: float):
    """Several still images → ONE Drawing2 with one frame per image.
    Fit mode: every frame is sized like the first image (scaled).
    No-scale mode: every frame keeps its own pixel size."""
    frames: list[bytes] = []
    sizes: list[tuple[int, int]] = []

    if st["no_scale"]:
        for f in files:
            pb(f"Loading image: {f.name}")
            try:
                w, h = visual_native_size(f, "image", ico_sizes.get(f))
                got, _ = load_image_frames(f, w, h, ico_size=ico_sizes.get(f))
            except Exception as e:
                pw(f"Skipping {f.name}: {e}")
                continue
            frames.extend(got)
            sizes.extend([(w, h)] * len(got))
        if not frames:
            raise ValueError("none of the images could be read")
        width, height = max(s[0] for s in sizes), max(s[1] for s in sizes)
    else:
        width = height = None
        for f in files:
            pb(f"Loading image: {f.name}")
            try:
                if width is None:   # sized like the first image that loads
                    width, height = scaled_size(
                        visual_native_size(f, "image", ico_sizes.get(f)), st["scale"])
                got, _ = load_image_frames(f, width, height, ico_size=ico_sizes.get(f),
                                           smooth=st["smooth"])
            except Exception as e:
                pw(f"Skipping {f.name}: {e}")
                continue
            frames.extend(got)
        if not frames:
            raise ValueError("none of the images could be read")
        sizes = None

    if st["quantize"]:
        pb(f"Quantizing {len(frames)} frame(s) to {st['quantize']} colors...")
        frames = [quantize_frame(f, st["quantize"]) for f in frames]
    play_mode = "loop" if len(frames) > 1 else "still"
    drawing2 = build_drawing2(frames, fps, width, height, play_mode,
                              sizes=sizes, scale=st["fixed_scale"])
    size_note = "sizes kept" if st["no_scale"] else f"{width}×{height}px"
    return drawing2, f"Image ready: {len(frames)} frame(s), {fps} FPS, {size_note}"


def render_svg_frames_actor(files: list[Path], st: dict, fps: float):
    """Several SVGs → ONE Drawing2 with one frame per SVG. Returns
    (drawing2, summary). Bitmap SVGs follow the same fit / don't-scale rules
    as still images."""
    if not st["bitmap"]:
        items = []
        for f in files:
            pb(f"Loading SVG: {f.name}")
            try:
                items.append(svg_to_path_data(f, steps=st["steps"], scale=st["svg_scale"],
                                              color=st["color"], fallback_color=st["fallback_color"]))
            except Exception as e:
                pw(f"Skipping {f.name}: {e}")
        if not items:
            raise ValueError("none of the SVGs could be read")
        play_mode = "loop" if len(items) > 1 else "still"
        drawing2 = build_drawing2_vector_frames(items, fps=fps, play_mode=play_mode)
        return drawing2, f"SVG ready: {len(items)} frame(s), {fps} FPS"

    rendered = []   # (png, w, h)
    for f in files:
        pb(f"Rasterizing SVG: {f.name}")
        try:
            rendered.append(rasterize_svg(f, scale=st["scale"], smooth=st["smooth"],
                                          color=st["color"], fallback_color=st["fallback_color"]))
        except Exception as e:
            pw(f"Skipping {f.name}: {e}")
    if not rendered:
        raise ValueError("none of the SVGs could be read")
    if st["no_scale"]:
        frames = [r[0] for r in rendered]
        sizes = [(r[1], r[2]) for r in rendered]
        width, height = max(s[0] for s in sizes), max(s[1] for s in sizes)
    else:
        width, height = rendered[0][1], rendered[0][2]
        resample = Image.BILINEAR if st["smooth"] else Image.NEAREST
        frames = []
        for png, w, h in rendered:
            if (w, h) != (width, height):
                im = Image.open(io.BytesIO(png)).convert("RGBA").resize((width, height), resample)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                png = buf.getvalue()
            frames.append(png)
        sizes = None
    if st["quantize"]:
        pb(f"Quantizing {len(frames)} frame(s) to {st['quantize']} colors...")
        frames = [quantize_frame(f, st["quantize"]) for f in frames]
    play_mode = "loop" if len(frames) > 1 else "still"
    drawing2 = build_drawing2(frames, fps, width, height, play_mode,
                              sizes=sizes, scale=st["fixed_scale"])
    size_note = "sizes kept" if st["no_scale"] else f"{width}×{height}px"
    return drawing2, f"SVG ready: bitmap, {len(frames)} frame(s), {fps} FPS, {size_note}"


def do_add_image(bp_path: Path, actor: dict, card: Path):
    while True:
        raw = ask_path("Enter the file path for your image/video (or a .zip/.tar archive of them)")
        src = resolve_path(raw)
        if src.exists():
            break
        pe(f"File not found: {src}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = gather_media(src, tmp_dir)
        if not found:
            return
        files, cat = found
        if not check_category(cat, "Add image"):
            return
        files = usable_files(files)
        if not files:
            return
        if cat == "video" and not HAS_FFMPEG:
            pe("ffmpeg is required for video files. Install with: pkg install ffmpeg")
            return

        multi = len(files) > 1
        ico_sizes = pick_ico_sizes(files)
        if any(f.suffix.lower() == ".ico" for f in files) and \
           any(f.suffix.lower() == ".ico" and f not in ico_sizes for f in files):
            return  # an .ico couldn't be read; the error was already shown

        # still images / SVGs: the user decides how an archive is laid out
        frames_mode = False
        if cat in ("image", "vector") and multi:
            noun = "image" if cat == "image" else "SVG"
            choice = choose(f"Found {len(files)} {noun}s. How should they be added?", [
                f"All in this actor, one frame per {noun}",
                "Each as its own actor (forked from this one)",
            ])
            frames_mode = choice.startswith("All")
        elif multi:
            pb(f"Each of the {len(files)} {CATEGORY_LABELS[cat]} files will become its own actor.")

        st = prompt_visual_settings(cat, files, multi, frames_mode=frames_mode)
        if st["no_scale"]:
            st["fixed_scale"] = fixed_density_scale(files, ico_sizes, cat, st["scale"])

        fps = 4.0
        if frames_mode:
            while True:
                raw = ask("Frames per second", default="4")
                try:
                    fps = float(raw)
                    if fps > 0:
                        break
                except ValueError:
                    pass
                pe("Enter a number greater than 0.")

        # sound: decide once, up front, for videos
        want_audio = False
        audio_files: set[Path] = set()
        if cat == "video":
            audio_files = {f for f in files if probe_has_audio(f)}
            if audio_files:
                p()
                if len(files) == 1:
                    msg = "This video has audio. Upload it and play it with the actor?"
                else:
                    msg = (f"{len(audio_files)} of {len(files)} videos have audio. "
                           "Upload it and play it with each actor?")
                want_audio = yn(msg, default="y")

        base = copy.deepcopy(actor)
        p()

        if frames_mode:
            try:
                if cat == "vector":
                    drawing2, summary = render_svg_frames_actor(files, st, fps)
                else:
                    drawing2, summary = render_frames_actor(files, st, ico_sizes, fps)
            except (Exception, SystemExit) as e:
                pe(f"Could not import {'SVGs' if cat == 'vector' else 'images'}: {e}")
                return
            actor["actorBlueprint"]["components"]["Drawing2"] = drawing2
            ps(summary)
            write_actor(bp_path, actor)
            ps("Image added.")
            maybe_delete_placeholder_script(card, bp_path)
            return

        used_selected = False
        added = 0
        for f in files:
            try:
                drawing2, summary = render_visual(f, cat, st, ico_sizes.get(f))
            except (Exception, SystemExit) as e:
                pe(f"{f.name}: {e}")
                continue
            t_path, t_actor, is_selected = next_target(
                card, bp_path, actor, base, used_selected, f.stem)
            used_selected = True
            if not is_selected:
                strip_audio_rules(t_actor)
            t_actor["actorBlueprint"]["components"]["Drawing2"] = drawing2
            ps(summary)
            if want_audio and f in audio_files:
                if not attach_audio(t_actor, f, is_video=True):
                    pw("Continuing with silent video.")
            write_actor(t_path, t_actor)
            if is_selected:
                maybe_delete_placeholder_script(card, bp_path)
            else:
                ps(f"New actor created: {t_path.name}")
            added += 1

        if added:
            ps("Image added." if added == 1 else f"{added} files added ({added - 1} new actor(s)).")
        else:
            pe("Nothing was added.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def do_add_audio(bp_path: Path, actor: dict, card: Path):
    while True:
        raw = ask_path("Enter the file path for your audio (or a .zip/.tar archive of them)")
        src = resolve_path(raw)
        if src.exists():
            break
        pe(f"File not found: {src}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = gather_media(src, tmp_dir)
        if not found:
            return
        files, cat = found
        if not check_category(cat, "Add audio"):
            return
        if len(files) > 1:
            pb(f"Each of the {len(files)} audio files will become its own actor.")

        base = copy.deepcopy(actor)
        used_selected = False
        added = 0
        p()
        for f in files:
            t_path, t_actor, is_selected = next_target(
                card, bp_path, actor, base, used_selected, f.stem)
            if not is_selected:
                strip_audio_rules(t_actor)
            if not attach_audio(t_actor, f, is_video=False):
                continue
            used_selected = True
            write_actor(t_path, t_actor)
            if not is_selected:
                ps(f"New actor created: {t_path.name}")
            added += 1

        if added:
            ps("Audio added." if added == 1 else f"{added} files added ({added - 1} new actor(s)).")
        else:
            pe("Nothing was added.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def do_add_midi(bp_path: Path, actor: dict, card: Path):
    while True:
        raw = ask_path("Enter your MIDI file path")
        midi_path = resolve_path(raw)
        if midi_path.exists():
            break
        pe(f"File not found: {midi_path}")

    p()
    pb(f"Loading MIDI: {midi_path.name}")
    mid = mido.MidiFile(midi_path, clip=True)
    music = build_music(mid)
    pat_count = len(music["song"]["patterns"])
    trk_count = len(music["song"]["tracks"])
    ps(f"MIDI ready: {pat_count} patterns, {trk_count} tracks")

    actor["actorBlueprint"]["components"]["Music"] = music
    with open(bp_path, "w", encoding="utf-8") as f:
        json.dump(actor, f, indent=2)

    bpm = get_bpm(mid)
    try:
        set_card_tempo(card, bpm)
        ps(f"Card tempo set to {bpm:.2f} BPM.")
    except FileNotFoundError:
        pw(f"Could not find card.json in {card} — tempo was not updated.")

    ps("MIDI added.")


def eot_to_sfnt(raw: bytes) -> bytes:
    """Pull the TrueType/OpenType data out of an Embedded OpenType (.eot)
    wrapper. Handles plain and XOR-obfuscated files; MTX-compressed ones
    can't be unpacked here."""
    if len(raw) < 82:
        raise ValueError("file is too small to be an EOT font")
    eot_size, font_size, _version, flags = struct.unpack_from("<IIII", raw, 0)
    if flags & 0x4:
        raise ValueError("this EOT uses MTX compression, which isn't supported "
                         "(convert it to TTF or WOFF first)")
    end = eot_size if 0 < eot_size <= len(raw) else len(raw)
    if font_size <= 0 or font_size > end:
        raise ValueError("EOT header is corrupt")
    data = raw[end - font_size:end]
    if flags & 0x10000000:
        data = data.translate(bytes(b ^ 0x50 for b in range(256)))
    return data


def load_font(path: Path):
    """Open .ttf/.otf/.woff directly, and .eot via its wrapper."""
    if path.suffix.lower() == ".eot":
        return TTFont(io.BytesIO(eot_to_sfnt(path.read_bytes())))
    return TTFont(path)


def font_sfnt_bytes(fp: Path, font) -> bytes:
    """Plain TrueType/OpenType bytes for a font file (unwraps .eot, unpacks .woff)."""
    ext = fp.suffix.lower()
    if ext in (".ttf", ".otf"):
        return fp.read_bytes()
    if ext == ".eot":
        return eot_to_sfnt(fp.read_bytes())
    buf = io.BytesIO()
    font.flavor = None
    font.save(buf)
    return buf.getvalue()


def render_font_bitmap(fp: Path, font, codepoints: list[int], px: int,
                       smooth: bool, color: list):
    """Render each requested glyph as a filled RGBA image with FreeType.
    Every frame shares one canvas and one pen origin (like the vector fonts),
    so swapping frames never makes the glyph jump. `smooth` = anti-aliased
    edges; off = hard pixels. Returns (png_frames, (w, h), missing_count)."""
    from PIL import ImageDraw, ImageFont
    cmap, glyph_set = font.getBestCmap(), font.getGlyphSet()
    present = []
    missing = 0
    for cp in codepoints:
        name = cmap.get(cp)
        if not name or name not in glyph_set:
            missing += 1
        else:
            present.append(cp)
    if not present:
        return [], (0, 0), missing

    try:
        ft = ImageFont.truetype(io.BytesIO(font_sfnt_bytes(fp, font)), size=px)
    except Exception as e:
        raise RuntimeError(f"Pillow couldn't load this font for bitmap rendering ({e}). "
                           "It may be missing FreeType support.")

    boxes = [ft.getbbox(chr(cp), anchor="ls") for cp in present]
    pad = 1
    min_x, min_y = min(b[0] for b in boxes), min(b[1] for b in boxes)
    max_x, max_y = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width, height = max(1, max_x - min_x + 2 * pad), max(1, max_y - min_y + 2 * pad)
    origin = (pad - min_x, pad - min_y)

    rgb = tuple(round(c * 255) for c in color[:3])
    alpha = color[3] if len(color) > 3 else 1.0
    frames = []
    for cp in present:
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        draw.fontmode = "L" if smooth else "1"
        draw.text(origin, chr(cp), font=ft, fill=255, anchor="ls")
        if alpha < 1:
            mask = mask.point(lambda v: int(v * alpha))
        layer = Image.new("RGBA", (width, height), rgb + (0,))
        layer.putalpha(mask)
        buf = io.BytesIO()
        layer.save(buf, format="PNG")
        frames.append(buf.getvalue())
    return frames, (width, height), missing


def do_add_font(bp_path: Path, actor: dict, card: Path):
    while True:
        raw = ask_path("Enter the font file path (.ttf, .otf, .woff or .eot, or a .zip/.tar archive of them)")
        src = resolve_path(raw)
        if src.exists():
            break
        pe(f"File not found: {src}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = gather_media(src, tmp_dir)
        if not found:
            return
        files, cat = found
        if not check_category(cat, "Add Font"):
            return

        p()
        fonts = []
        for fp in files:
            pb(f"Loading font: {fp.name}")
            try:
                font = load_font(fp)
                if not font.getBestCmap():
                    pe(f"{fp.name}: this font has no usable character map (cmap).")
                    continue
                font.getGlyphSet()
                font["head"].unitsPerEm
            except Exception as e:
                pe(f"Could not read font file {fp.name}: {e}")
                continue
            fonts.append((fp, font))
        if not fonts:
            return
        if len(fonts) > 1:
            pb(f"Each of the {len(fonts)} fonts will become its own actor "
               "(same settings for all).")

        charset_names = list(FONT_CHARSETS.keys())
        mode = choose("Select a mode:", [
            "Basic (choose from preset unicode ranges)",
            "Advanced (choose exact characters/codepoints)",
        ])

        codepoints: list[int] = []
        label = ""
        if mode.startswith("Basic"):
            chosen_sets = multi_choose("Select one or more character sets:", charset_names)
            cp_set: set[int] = set()
            for name in chosen_sets:
                cp_set.update(FONT_CHARSETS[name]())
            codepoints = sorted(cp_set)
            label = ", ".join(chosen_sets)
        else:
            source = choose("How would you like to provide characters?",
                             ["Type them in", "Load from a file"])
            if source == "Type them in":
                text = ask_charspec(
                    "Enter characters and/or codepoints "
                    "(e.g. 0123456789ABCDEF U+03A9), separated by spaces:"
                )
            else:
                while True:
                    raw = ask_path("Enter the path to your character/codepoint file")
                    file_path = resolve_path(raw)
                    if file_path.exists():
                        break
                    pe(f"File not found: {file_path}")
                text = read_charspec_file(file_path)
                print("  " + charspec_preview_ansi(text))
                _tokens, _seen, valid = parse_charspec_tokens(text)
                if not valid:
                    pe("This file has duplicate or malformed characters/codepoints "
                       "(highlighted above). Fix it and try again.")
                    return
            codepoints = charspec_codepoints(text)
            label = "custom selection"

        if not codepoints:
            pe("No characters/codepoints were selected.")
            return

        style = choose("How should the glyphs be drawn?", [
            "Vector (outline line segments)",
            "Bitmap (filled in, one image per glyph)",
        ])
        bitmap = style.startswith("Bitmap")

        p()
        scale, steps, px, smooth = 1.0, 16, 64, False
        if bitmap:
            while True:
                raw = ask("Glyph size in pixels per em (bigger = sharper, larger file)", default="64")
                if raw.isdigit() and 4 <= int(raw) <= 512:
                    px = int(raw)
                    break
                pe("Enter a whole number between 4 and 512.")
            smooth = yn(SMOOTH_PROMPT, default="n")
        else:
            raw = ask("Scale multiplier (1.0 = ten Castle units per em)", default="1.0")
            try:
                scale = float(raw)
                if scale <= 0:
                    raise ValueError
            except ValueError:
                pw("Invalid scale, using 1.0.")
                scale = 1.0

            if yn("Customize bezier curve smoothness? (default 16 steps)", default="n"):
                while True:
                    raw = ask("Steps per curve segment", default="16")
                    if raw.isdigit() and int(raw) >= 2:
                        steps = int(raw)
                        break
                    pe("Enter a number ≥ 2")

        color = ask_color("Pick the glyph color")

        base = copy.deepcopy(actor)
        used_selected = False
        added = 0
        for fp, font in fonts:
            p()
            pb(f"Rendering {len(codepoints)} glyph(s) from '{label}' ({fp.name})...")
            if bitmap:
                try:
                    frames, (gw, gh), missing = render_font_bitmap(
                        fp, font, codepoints, px, smooth, color)
                except (Exception, SystemExit) as e:
                    pe(f"{fp.name}: {e}")
                    continue
                count = len(frames)
                if count:
                    drawing2 = build_drawing2(frames, 4, gw, gh, "still")
            else:
                cmap = font.getBestCmap()
                glyph_set = font.getGlyphSet()
                ppu = font["head"].unitsPerEm / 10.0  # 1 em = 10 Castle units before the user's scale multiplier
                glyph_frames = []
                missing = 0
                for cp in codepoints:
                    glyph_name = cmap.get(cp)
                    if not glyph_name or glyph_name not in glyph_set:
                        missing += 1
                        continue
                    d = font_glyph_svg_d(glyph_set, glyph_name)
                    path_data, bounds = font_glyph_to_path_data(d, steps, ppu, scale, color)
                    glyph_frames.append((cp, path_data, bounds))
                count = len(glyph_frames)
                if count:
                    drawing2 = build_drawing2_font(glyph_frames, ppu, scale=10)

            if not count:
                pe(f"None of the requested codepoints have a glyph in {fp.name}.")
                continue

            t_path, t_actor, is_selected = next_target(
                card, bp_path, actor, base, used_selected, fp.stem)
            used_selected = True
            t_actor["actorBlueprint"]["components"]["Drawing2"] = drawing2
            write_actor(t_path, t_actor)

            if missing:
                pw(f"{missing} requested codepoint(s) have no glyph in this font and were skipped.")
            ps(f"Font added: {count} frame(s), one per glyph (frame order matches codepoint order).")
            if is_selected:
                maybe_delete_placeholder_script(card, bp_path)
            else:
                ps(f"New actor created: {t_path.name}")
            added += 1

        if not added:
            pe("Nothing was added.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _env_flag(name: str) -> bool:
    """True if an env var is set to a truthy value (unset/'0'/'false'/'' = off)."""
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def _minify_deck_json(deck: Path) -> None:
    """Rewrite every .json file under the deck without insignificant
    whitespace. Best-effort and silent: a file that can't be read or parsed
    is left exactly as it was, and each rewrite is atomic."""
    for root, dirs, files in os.walk(deck):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(".json"):
                continue
            path = Path(root) / name
            tmp = path.with_name(path.name + ".tmp")
            try:
                original = path.read_text(encoding="utf-8-sig")
                compact = json.dumps(json.loads(original),
                                     separators=(",", ":"),
                                     ensure_ascii=False)
                if compact == original:
                    continue
                tmp.write_text(compact, encoding="utf-8", newline="")
                os.replace(tmp, path)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass


def do_upload_deck(deck: Path):
    _minify_deck_json(deck)
    castle_argv = ["castle", "save-deck", str(deck)]
    if os.name == "nt":
        # On Windows, CreateProcess (used when shell=False) only tries
        # appending .exe to a bare command name — it doesn't check
        # PATHEXT the way a real shell does. Most CLI tools (including
        # Castle's) install as a .cmd shim, so a bare "castle" call
        # fails with WinError 2 even though it's on PATH and works fine
        # when typed into a terminal. Routing through cmd.exe /c gives
        # it the same PATHEXT-aware resolution the shell uses.
        castle_argv = ["cmd", "/c", *castle_argv]
    try:
        result = subprocess.run(castle_argv, capture_output=True, text=True)
    except FileNotFoundError:
        pe("Could not find the 'castle' CLI. Is it installed and on your PATH?")
        return
    combined = (result.stdout + result.stderr).lower()
    if result.returncode == 0:
        ps("Saved!")
        for line in result.stdout.strip().splitlines():
            print(f"   {line}")
    elif "memory access out of bounds" in combined:
        pe("Your file is too large!")
    elif "you do not have permission to edit this deck" in combined:
        pe("Permission denied, you likely deleted the deck on the app")
    else:
        pe("Save failed:")
        print(result.stdout)
        print(result.stderr)


def main():
    if "--version" in sys.argv[1:] or "-v" in sys.argv[1:]:
        print(f"castletool {get_installed_version()}")
        return

    p()
    pb("═══════════════")
    pb("   Castletool   ")
    pb("═══════════════")
    p()

    check_for_update()
    ensure_dependencies()
    p()

    home = Path.cwd()
    deck = card = bp_path = actor = None
    stage = "deck"

    while True:
        if stage == "deck":
            decks = find_decks(home)
            if not decks:
                pe("No decks were found. Are you in the right directory?")
                sys.exit(1)
            deck_names = [d.name for d in decks]
            pb(f"Detected decks: {', '.join(deck_names)}")
            if len(decks) == 1:
                deck = decks[0]
                ps(f"Auto selecting {deck.name}")
            else:
                chosen = choose("Select the deck you would like to modify:",
                                 [*deck_names, "Exit Tool"])
                if chosen == "Exit Tool":
                    return
                deck = home / chosen
            p()
            stage = "card"
            continue

        if stage == "card":
            cards = find_cards(deck)
            if not cards:
                pe(f"No cards found in {deck}.")
                sys.exit(1)
            card_names = [c.name for c in cards]
            if len(cards) == 1:
                card = cards[0]
                ps(f"Auto selecting {card.name}")
            else:
                chosen = choose("Select the card you would like to edit:",
                                 [*card_names, "« Change Deck", "Exit Tool"])
                if chosen == "Exit Tool":
                    return
                if chosen == "« Change Deck":
                    stage = "deck"
                    continue
                card = deck / "cards" / chosen
            p()
            stage = "blueprint"
            continue

        if stage == "blueprint":
            blueprints = find_blueprints(card)
            if not blueprints:
                pe(f"No blueprints found in {card}.")
                sys.exit(1)
            bp_names = [b.name for b in blueprints]
            if len(blueprints) == 1:
                bp_path = blueprints[0]
                ps(f"Auto selecting {bp_path.name}")
            else:
                chosen = choose("Select the actor you want to edit:",
                                 [*bp_names, "« Change Card", "Exit Tool"])
                if chosen == "Exit Tool":
                    return
                if chosen == "« Change Card":
                    stage = "card"
                    continue
                bp_path = card / "scene" / "blueprints" / chosen
            p()

            with open(bp_path, "r", encoding="utf-8") as f:
                actor = json.load(f)
            stage = "action"
            continue

        # ── action menu ──
        options = []
        if HAS_PIL:
            options.append("Add image")
        options.append("Add audio")
        if HAS_MIDO:
            options.append("Add MIDI")
        if HAS_FONTTOOLS:
            options.append("Add Font")
        options.append("Edit Background Color")
        options.append("Upload Deck")
        options.append("« Change Blueprint")
        options.append("Exit Tool")

        action = choose("Select the action you want to perform:", options)

        if action == "Add image":
            do_add_image(bp_path, actor, card)
        elif action == "Add audio":
            do_add_audio(bp_path, actor, card)
        elif action == "Add MIDI":
            do_add_midi(bp_path, actor, card)
        elif action == "Add Font":
            do_add_font(bp_path, actor, card)
        elif action == "Edit Background Color":
            do_edit_background_color(card)
        elif action == "Upload Deck":
            do_upload_deck(deck)
        elif action == "« Change Blueprint":
            stage = "blueprint"
        elif action == "Exit Tool":
            p()
            return


if __name__ == "__main__":
    main()
