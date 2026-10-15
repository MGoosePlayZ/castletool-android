"""Blueprint editing operations: the non-interactive half of castletool.

castletool_core.py is an unmodified copy of castletool/castletool.py. Its
prompts live in do_add_* flows; the conversion functions underneath
(render_visual, build_music, svg_to_path_data, ...) are pure, so this module
re-implements only the "flow" layer with explicit parameters instead of input().

Everything here blocks: call from a worker thread.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

import castletool_core as ct
import drawing
import media_av

media_av.install(ct)   # PyAV instead of ffmpeg/ffprobe subprocesses (works on phones)

Log = Callable[[str], None]

_state = {"token": None}
_patch_lock = threading.Lock()


class OpError(Exception):
    """Something the user should read (shown in the UI as-is)."""


# ── plumbing ─────────────────────────────────────────────────────────────

def _hook(log: Log, token: Optional[str]) -> None:
    """Route castletool's print helpers to `log` and its token lookup to ours."""
    clean = lambda m="": log(re.sub(r"\x1b\[[0-9;]*m", "", str(m)))
    ct.p = clean
    ct.pb = clean
    ct.pw = lambda m: log(f"warning: {m}")
    ct.pe = lambda m: log(f"error: {m}")
    ct.ps = clean
    _state["token"] = token

    def get_token() -> str:
        if not _state["token"]:
            raise RuntimeError("Not signed in. Sign in first.")
        return _state["token"]

    ct.get_castle_token = get_token


def _guard(fn):
    """Serialize ops (they patch module globals) and turn SystemExit into OpError."""
    def wrapper(*args, log: Log = print, token: Optional[str] = None, **kwargs):
        with _patch_lock:
            _hook(log, token)
            try:
                return fn(*args, log=log, **kwargs)
            except SystemExit as e:
                raise OpError(str(e.code) if e.code not in (None, 0, 1) else "Operation failed (see log).")
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def load_blueprint(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_blueprint(path: Path, bp: dict) -> None:
    ct.write_actor(Path(path), bp)


def blueprint_title(bp: dict, fallback: str = "") -> str:
    return bp.get("title") or fallback


def components(bp: dict) -> dict:
    return (bp.get("actorBlueprint") or {}).get("components") or {}


def ensure_components(bp: dict) -> dict:
    ab = bp.setdefault("actorBlueprint", {})
    return ab.setdefault("components", {})


# ── capability flags (for the UI to explain what's unavailable) ──────────

def capabilities() -> dict:
    return {
        "pillow": ct.HAS_PIL,
        "mido": ct.HAS_MIDO,
        "fonttools": ct.HAS_FONTTOOLS,
        "media": ct.HAS_FFMPEG,                 # video/audio decoding via PyAV
        "mp3_encode": media_av.can_encode_mp3(),
    }


# ── inspect a picked file / archive ──────────────────────────────────────

CATEGORY_LABELS = ct.CATEGORY_LABELS


@_guard
def inspect_media(src: Path, *, log: Log) -> dict:
    """What is this file (or archive of files)? Returns
    {category, count, names, needs_ffmpeg, has_audio, size}. Raises OpError."""
    src = Path(src)
    if not src.exists():
        raise OpError(f"File not found: {src}")
    tmp = Path(tempfile.mkdtemp(prefix="castletool-inspect-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found:
            raise OpError("No usable files found (see log).")
        files, cat = found
        has_audio = False
        if cat == "video" and ct.HAS_FFMPEG:
            has_audio = any(ct.probe_has_audio(f) for f in files)
        unsupported = ""
        if cat == "video" and not ct.HAS_FFMPEG:
            unsupported = "Video isn't supported on this device."
        elif cat == "audio" and not all(f.suffix.lower() == ".mp3" for f in files) \
                and not media_av.can_encode_mp3():
            unsupported = "Only .mp3 files can be added on this device."
        return {
            "category": cat,
            "count": len(files),
            "names": [f.name for f in files][:50],
            "unsupported": unsupported,
            "has_audio": has_audio,
            "size": max(f.stat().st_size for f in files),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── media (image / animation / video / svg) ──────────────────────────────

DEFAULT_MEDIA_OPTS = {
    "layout": "frames",       # multi-file: "frames" (one actor, frame per file) | "separate" (fork per file)
    "no_scale": False,        # multi-file stills: keep 1 px = 1 px
    "scale": 1.0,             # bitmap scale
    "smooth": False,
    "every": 1,               # keep every Nth frame (animations / video)
    "quantize": 0,            # 0 = off, else colors 1..256
    "svg_style": "vector",    # "vector" | "bitmap"
    "svg_scale": 1.0,         # vector-style scale multiplier
    "steps": 16,              # bezier steps
    "color": None,            # override color [r,g,b,a] 0..1
    "fallback_color": None,
    "fps": 4.0,               # frame-per-file layouts
    "want_audio": True,       # videos: upload sound
}


def parse_color(text: str, default=None) -> Optional[list]:
    """'#rrggbb' / 'rrggbb' → [r,g,b,1] (0..1)."""
    t = (text or "").strip().lstrip("#")
    if not t:
        return default
    if not re.fullmatch(r"[0-9a-fA-F]{6}", t):
        raise OpError("Colors are 6-digit hex like #ff8800.")
    return [round(int(t[i:i + 2], 16) / 255, 5) for i in (0, 2, 4)] + [1]


def _ico_sizes(files) -> dict:
    out = {}
    for f in files:
        if f.suffix.lower() == ".ico":
            try:
                out[f] = ct.ico_sizes_of(f)[0]  # largest
            except Exception as e:
                raise OpError(f"Could not read {f.name}: {e}")
    return out


# 1 em = 10 Castle units, so the Scale box means "ems": scale 1 -> 10 units per em.
SVG_UNITS_PER_EM = 10.0


def _media_state(o: dict, cat: str, files, ico_sizes: dict, multi: bool) -> dict:
    bitmap = cat == "vector" and o["svg_style"] == "bitmap"
    st = {
        "scale": float(o["scale"]) if (cat != "vector" or bitmap) else 1.0,
        "every": max(1, int(o["every"])),
        "quantize": int(o["quantize"] or 0),
        "svg_scale": float(o["svg_scale"]) * SVG_UNITS_PER_EM,
        "steps": max(2, int(o["steps"])),
        "color": o["color"],
        "smooth": bool(o["smooth"]),
        "bitmap": bitmap,
        "fallback_color": o["fallback_color"],
        "no_scale": bool(o["no_scale"]) and multi and cat in ("image", "vector") and (cat == "image" or bitmap),
        "fixed_scale": None,
    }
    if st["scale"] <= 0 or float(o["svg_scale"]) <= 0:
        raise OpError("Scale must be greater than 0.")
    if cat == "vector" and not st["color"] and not st["fallback_color"]:
        if any(ct.svg_needs_fallback_color(f, st["bitmap"]) for f in files):
            st["fallback_color"] = [0, 0, 0, 1]  # castletool asks; default to black
    if st["no_scale"]:
        st["fixed_scale"] = ct.fixed_density_scale(files, ico_sizes, cat, st["scale"])
    return st


@_guard
def preview_media(src: Path, opts: dict, *, log: Log, bg=None, size: int = 220) -> Optional[bytes]:
    """PNG of what Import would draw (first file, first frame), for the import modal."""
    o = {**DEFAULT_MEDIA_OPTS, **(opts or {})}
    src = Path(src)
    tmp = Path(tempfile.mkdtemp(prefix="castletool-preview-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found:
            return None
        files, cat = found
        files = ct.usable_files(files)
        if not files or cat in ("audio", "font"):
            return None
        if cat == "video" and not ct.HAS_FFMPEG:
            return None
        ico = _ico_sizes(files[:1])
        st = _media_state(o, cat, files[:1], ico, False)
        d2, _summary = ct.render_visual(files[0], cat, st, ico.get(files[0]))
        return drawing.render_png(d2, size, bg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@_guard
def preview_font(src: Path, opts: dict, *, log: Log, bg=None, size: int = 220) -> Optional[bytes]:
    """PNG of the first selected glyph with the chosen settings."""
    src = Path(src)
    if not ct.HAS_FONTTOOLS:
        return None
    cps = _font_codepoints(opts)
    if not cps:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="castletool-preview-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found or found[1] != "font":
            return None
        fp = found[0][0]
        font = ct.load_font(fp)
        sample = [cps[0]]
        color = opts.get("color") or [1, 1, 1, 1]
        if opts.get("bitmap"):
            frames, (gw, gh), _missing = ct.render_font_bitmap(
                fp, font, sample, int(opts.get("px") or 64), bool(opts.get("smooth")), color)
            if not frames:
                return None
            d2 = ct.build_drawing2(frames, 4, gw, gh, "still")
        else:
            cmap, gs = font.getBestCmap(), font.getGlyphSet()
            name = cmap.get(sample[0])
            if not name or name not in gs:
                return None
            ppu = font["head"].unitsPerEm / 10.0
            pdata, bounds = ct.font_glyph_to_path_data(
                ct.font_glyph_svg_d(gs, name), max(2, int(opts.get("steps") or 16)), ppu,
                float(opts.get("scale") or 1.0), color)
            d2 = ct.build_drawing2_font([(sample[0], pdata, bounds)], ppu, scale=10)
        return drawing.render_png(d2, size, bg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _font_codepoints(opts: dict) -> list[int]:
    cps: set[int] = set()
    for name in opts.get("charsets") or []:
        if name not in ct.FONT_CHARSETS:
            raise OpError(f"Unknown character set: {name}")
        cps.update(ct.FONT_CHARSETS[name]())
    custom = (opts.get("custom") or "").strip()
    if custom:
        _t, _s, valid = ct.parse_charspec_tokens(custom)
        if not valid:
            raise OpError("Characters/codepoints are duplicated or malformed.")
        cps.update(ct.charspec_codepoints(custom))
    return sorted(cps)


@_guard
def import_media(bp_path: Path, card_dir: Path, src: Path, opts: dict, *, log: Log) -> list[Path]:
    """Port of do_add_image(). Returns the blueprint files written."""
    o = {**DEFAULT_MEDIA_OPTS, **(opts or {})}
    bp_path, card_dir, src = Path(bp_path), Path(card_dir), Path(src)
    if not ct.HAS_PIL:
        raise OpError("Pillow is not installed.")
    actor = load_blueprint(bp_path)
    ensure_components(actor)

    tmp = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found:
            raise OpError("No usable files found.")
        files, cat = found
        if not ct.check_category(cat, "Add image"):
            raise OpError(f"That is a {CATEGORY_LABELS[cat]} file; use the matching section.")
        files = ct.usable_files(files)
        if not files:
            raise OpError("Nothing left to import.")
        if cat == "video" and not ct.HAS_FFMPEG:
            raise OpError("Video isn't supported on this device.")

        multi = len(files) > 1
        ico_sizes = _ico_sizes(files)
        frames_mode = multi and cat in ("image", "vector") and o["layout"] == "frames"
        st = _media_state(o, cat, files, ico_sizes, multi)

        fps = float(o["fps"]) if frames_mode else 4.0
        if frames_mode and fps <= 0:
            raise OpError("FPS must be greater than 0.")

        audio_files = set()
        want_audio = False
        if cat == "video" and o["want_audio"]:
            audio_files = {f for f in files if ct.probe_has_audio(f)}
            want_audio = bool(audio_files)

        base = copy.deepcopy(actor)
        written: list[Path] = []

        if frames_mode:
            try:
                if cat == "vector":
                    d2, summary = ct.render_svg_frames_actor(files, st, fps)
                else:
                    d2, summary = ct.render_frames_actor(files, st, ico_sizes, fps)
            except Exception as e:
                raise OpError(f"Could not import: {e}")
            actor["actorBlueprint"]["components"]["Drawing2"] = d2
            log(summary)
            write_blueprint(bp_path, actor)
            ct.maybe_delete_placeholder_script(card_dir, bp_path)
            return [bp_path]

        used_selected = False
        for f in files:
            try:
                d2, summary = ct.render_visual(f, cat, st, ico_sizes.get(f))
            except Exception as e:
                log(f"error: {f.name}: {e}")
                continue
            t_path, t_actor, is_sel = ct.next_target(card_dir, bp_path, actor, base, used_selected, f.stem)
            used_selected = True
            if not is_sel:
                ct.strip_audio_rules(t_actor)
            t_actor["actorBlueprint"]["components"]["Drawing2"] = d2
            log(summary)
            if want_audio and f in audio_files:
                if not ct.attach_audio(t_actor, f, is_video=True):
                    log("warning: continuing with silent video.")
            write_blueprint(t_path, t_actor)
            if is_sel:
                ct.maybe_delete_placeholder_script(card_dir, bp_path)
            else:
                log(f"New actor created: {t_path.name}")
            written.append(t_path)
        if not written:
            raise OpError("Nothing was added.")
        return written
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── fonts ────────────────────────────────────────────────────────────────

def font_charset_names() -> list[str]:
    return list(ct.FONT_CHARSETS.keys())


@_guard
def import_font(bp_path: Path, card_dir: Path, src: Path, opts: dict, *, log: Log) -> list[Path]:
    """Port of do_add_font(). opts: charsets (list of preset names) and/or
    custom (charspec text), bitmap, scale, steps, px, smooth, color."""
    bp_path, card_dir, src = Path(bp_path), Path(card_dir), Path(src)
    if not ct.HAS_FONTTOOLS:
        raise OpError("fontTools is not installed.")
    actor = load_blueprint(bp_path)
    ensure_components(actor)

    codepoints = _font_codepoints(opts)
    if not codepoints:
        raise OpError("No characters were selected.")

    bitmap = bool(opts.get("bitmap"))
    scale = float(opts.get("scale") or 1.0)
    steps = max(2, int(opts.get("steps") or 16))
    px = int(opts.get("px") or 64)
    smooth = bool(opts.get("smooth"))
    color = opts.get("color") or [0, 0, 0, 1]
    if scale <= 0:
        raise OpError("Scale must be greater than 0.")
    if bitmap and not 4 <= px <= 512:
        raise OpError("Pixels per em must be between 4 and 512.")

    tmp = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found:
            raise OpError("No usable files found.")
        files, cat = found
        if not ct.check_category(cat, "Add Font"):
            raise OpError(f"That is a {CATEGORY_LABELS[cat]} file; use the matching section.")

        fonts = []
        for fp in files:
            try:
                font = ct.load_font(fp)
                if not font.getBestCmap():
                    log(f"error: {fp.name}: no usable character map.")
                    continue
                font.getGlyphSet()
                font["head"].unitsPerEm
            except Exception as e:
                log(f"error: could not read font {fp.name}: {e}")
                continue
            fonts.append((fp, font))
        if not fonts:
            raise OpError("No readable fonts.")

        base = copy.deepcopy(actor)
        used_selected = False
        written: list[Path] = []
        for fp, font in fonts:
            log(f"Rendering {len(codepoints)} glyph(s) from {fp.name}...")
            count, missing, d2 = 0, 0, None
            if bitmap:
                try:
                    frames, (gw, gh), missing = ct.render_font_bitmap(fp, font, codepoints, px, smooth, color)
                except Exception as e:
                    log(f"error: {fp.name}: {e}")
                    continue
                count = len(frames)
                if count:
                    d2 = ct.build_drawing2(frames, 4, gw, gh, "still")
            else:
                cmap, glyph_set = font.getBestCmap(), font.getGlyphSet()
                ppu = font["head"].unitsPerEm / 10.0  # 1 em = 10 Castle units
                glyph_frames = []
                for cp in codepoints:
                    name = cmap.get(cp)
                    if not name or name not in glyph_set:
                        missing += 1
                        continue
                    d = ct.font_glyph_svg_d(glyph_set, name)
                    pdata, bounds = ct.font_glyph_to_path_data(d, steps, ppu, scale, color)
                    glyph_frames.append((cp, pdata, bounds))
                count = len(glyph_frames)
                if count:
                    d2 = ct.build_drawing2_font(glyph_frames, ppu, scale=10)
            if not count:
                log(f"error: no requested codepoints have a glyph in {fp.name}.")
                continue
            t_path, t_actor, is_sel = ct.next_target(card_dir, bp_path, actor, base, used_selected, fp.stem)
            used_selected = True
            t_actor["actorBlueprint"]["components"]["Drawing2"] = d2
            write_blueprint(t_path, t_actor)
            if missing:
                log(f"warning: {missing} codepoint(s) have no glyph in this font and were skipped.")
            log(f"Font added: {count} frame(s), one per glyph.")
            if is_sel:
                ct.maybe_delete_placeholder_script(card_dir, bp_path)
            else:
                log(f"New actor created: {t_path.name}")
            written.append(t_path)
        if not written:
            raise OpError("Nothing was added.")
        return written
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── MIDI → Music ─────────────────────────────────────────────────────────

@_guard
def import_midi(bp_path: Path, card_dir: Path, midi_path: Path, *, log: Log) -> dict:
    """Port of do_add_midi(). Also sets the card tempo. Returns the Music dict."""
    if not ct.HAS_MIDO:
        raise OpError("mido is not installed.")
    midi_path = Path(midi_path)
    if not midi_path.exists():
        raise OpError(f"File not found: {midi_path}")
    try:
        mid = ct.mido.MidiFile(midi_path, clip=True)
    except Exception as e:
        raise OpError(f"Could not read MIDI: {e}")
    music = ct.build_music(mid)
    log(f"MIDI ready: {len(music['song']['patterns'])} patterns, {len(music['song']['tracks'])} tracks")
    actor = load_blueprint(bp_path)
    ensure_components(actor)["Music"] = music
    write_blueprint(bp_path, actor)
    bpm = ct.get_bpm(mid)
    try:
        ct.set_card_tempo(Path(card_dir), bpm)
        log(f"Card tempo set to {bpm:.2f} BPM.")
    except FileNotFoundError:
        log(f"warning: no card.json in {card_dir}; tempo not updated.")
    return music


# ── audio upload ─────────────────────────────────────────────────────────

AUDIO_RULE_NAME = "play sound"


def audio_urls(bp: dict) -> list[str]:
    """Upload URLs of the audio loop rule's play-sound chain, in play order."""
    rules = (components(bp).get("Rules") or {}).get("rules") or []
    urls: list[str] = []
    for rule in rules:
        if not ct.is_audio_loop_rule(rule):
            continue
        node = rule["response"]["params"].get("body")
        while isinstance(node, dict) and node.get("name") == AUDIO_RULE_NAME:
            params = node.get("params") or {}
            if params.get("uploadUrl"):
                urls.append(params["uploadUrl"])
            node = params.get("nextResponse")
    return urls


@_guard
def import_audio(bp_path: Path, card_dir: Path, src: Path, *, log: Log) -> list[Path]:
    """Port of do_add_audio(): upload each file, add a play-on-create loop rule."""
    bp_path, card_dir, src = Path(bp_path), Path(card_dir), Path(src)
    if not _state["token"]:
        raise OpError("Sign in first: audio is uploaded to Castle.")
    actor = load_blueprint(bp_path)
    ensure_components(actor)

    tmp = Path(tempfile.mkdtemp(prefix="castletool-media-"))
    try:
        found = ct.gather_media(src, tmp)
        if not found:
            raise OpError("No usable files found.")
        files, cat = found
        if not ct.check_category(cat, "Add audio"):
            raise OpError(f"That is a {CATEGORY_LABELS[cat]} file; use the matching section.")
        if not media_av.can_encode_mp3() and not all(f.suffix.lower() == ".mp3" for f in files):
            raise OpError("Only .mp3 files can be added on this device.")

        base = copy.deepcopy(actor)
        used_selected = False
        written: list[Path] = []
        for f in files:
            t_path, t_actor, is_sel = ct.next_target(card_dir, bp_path, actor, base, used_selected, f.stem)
            if not is_sel:
                ct.strip_audio_rules(t_actor)
            if not ct.attach_audio(t_actor, f, is_video=False):
                continue
            used_selected = True
            write_blueprint(t_path, t_actor)
            if not is_sel:
                log(f"New actor created: {t_path.name}")
            written.append(t_path)
        if not written:
            raise OpError("Nothing was added (see log).")
        return written
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── card settings ────────────────────────────────────────────────────────

def set_card_background(card_dir: Path, hex_color: str) -> str:
    """Port of do_edit_background_color(); also mirrors it into deck.json so the
    deck list shows the new color."""
    card_dir = Path(card_dir)
    raw = hex_color.strip()
    if not raw.startswith("#"):
        raw = "#" + raw
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        raise OpError("Enter a hex color like #09101a.")
    data = ct.load_card_json(card_dir)
    data["backgroundColor"] = raw
    data.setdefault("sceneProperties", {})["backgroundColor"] = ct.hex_to_rgba(raw)
    ct.save_card_json(card_dir, data)

    deck_json = card_dir.parent.parent / "deck.json"
    if deck_json.exists():
        deck = json.loads(deck_json.read_text(encoding="utf-8-sig"))
        cid = card_dir.name
        touched = False
        for c in [deck.get("initialCard")] + list(deck.get("cards") or []):
            if isinstance(c, dict) and c.get("cardId") == cid:
                c["backgroundColor"] = raw
                touched = True
        if touched:
            deck_json.write_text(json.dumps(deck, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return raw


def set_card_tempo(card_dir: Path, bpm: float) -> None:
    if bpm <= 0:
        raise OpError("Tempo must be greater than 0.")
    ct.set_card_tempo(Path(card_dir), float(bpm))


def card_tempo(card_dir: Path) -> float:
    try:
        return float(ct.load_card_json(Path(card_dir))["sceneProperties"]["clock"]["tempo"])
    except (OSError, KeyError, TypeError, ValueError):
        return 120.0
