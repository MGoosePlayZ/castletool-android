"""Blueprint editing operations: the non-interactive half of castletool.

castletool_core.py is an unmodified copy of castletool/castletool.py. Its
prompts live in do_add_* flows; the conversion functions underneath
(render_visual, build_music, svg_to_path_data, ...) are pure, so this module
re-implements only the "flow" layer with explicit parameters instead of input().

Everything here blocks: call from a worker thread.
"""
from __future__ import annotations

import copy
import io
import wave
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

import castletool_core as ct
import drawing
import live_synth
import layers as lyr
import media_av
import tasks

media_av.install(ct)   # PyAV instead of ffmpeg/ffprobe subprocesses (works on phones)

Log = Callable[[str], None]

_state = {"token": None, "errors": [], "warnings": []}
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
    ct.pe = lambda m: (_state["errors"].append(str(m)), log(f"error: {m}"))
    ct.ps = clean
    _state["token"] = token
    _state["errors"] = []

    def get_token() -> str:
        if not _state["token"]:
            raise RuntimeError("Not signed in. Sign in first.")
        return _state["token"]

    ct.get_castle_token = get_token


def _guard(fn):
    """Serialize ops (they patch module globals) and turn SystemExit into OpError."""
    def wrapper(*args, log: Log = print, token: Optional[str] = None, task: Optional[tasks.Task] = None, **kwargs):
        if task is not None:
            task.check()                       # cancelled while waiting for the lock? never start
        with _patch_lock:
            _hook(log, token)
            try:
                with tasks.use(task):
                    tasks.check()
                    return fn(*args, log=log, **kwargs)
            except SystemExit as e:
                raise OpError(str(e.code) if e.code not in (None, 0, 1) else "Operation failed (see log).")
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def take_warnings() -> list[str]:
    """Problems the last import hit but carried on past (e.g. a video's sound that couldn't be
    uploaded). Returns them once, for the UI to show."""
    out, _state["warnings"] = _state["warnings"], []
    return out


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
            "audio_unsupported": has_audio and not media_av.can_encode_mp3(),
            "size": max(f.stat().st_size for f in files),
            "frames": _frames_in(files[0], cat),      # frames inside the first file (animation / video)
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── layers ───────────────────────────────────────────────────────────────

def drawing_layers(bp: dict) -> list[dict]:
    """[{id, title, bitmap}] bottom layer first."""
    return lyr.summary(components(bp).get("Drawing2"))


def _put_drawing(actor: dict, d2: dict, layer: Optional[str]) -> None:
    """Store an imported drawing: replaces the Drawing2 (layer None) or goes into one layer."""
    comps = actor["actorBlueprint"]["components"]
    try:
        comps["Drawing2"] = lyr.apply_drawing(comps.get("Drawing2"), d2, layer)
    except lyr.LayerError as e:
        raise OpError(str(e))


def _edit_drawing(bp_path: Path, fn) -> None:
    bp = load_blueprint(bp_path)
    d2 = components(bp).get("Drawing2")
    if not d2 or not lyr.layers_of(d2):
        raise OpError("This blueprint has no drawing.")
    try:
        fn(d2)
    except lyr.LayerError as e:
        raise OpError(str(e))
    write_blueprint(bp_path, bp)


def rename_layer(bp_path: Path, layer_id: str, name: str) -> None:
    _edit_drawing(bp_path, lambda d2: lyr.rename_layer(d2, layer_id, name))


def delete_layer(bp_path: Path, layer_id: str) -> None:
    _edit_drawing(bp_path, lambda d2: lyr.delete_layer(d2, layer_id))


def move_layer(bp_path: Path, old_index: int, new_index: int) -> None:
    """Storage-order indices (0 = bottom layer)."""
    _edit_drawing(bp_path, lambda d2: lyr.move_layer(d2, old_index, new_index))


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
    "fixed_px": False,        # Fixed Pixel Size: every image pixel is `pixel_size` Castle units wide, never resampled
    "pixel_size": 0.1,
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
    fixed_px = bool(o.get("fixed_px")) and (cat != "vector" or bitmap) and cat not in ("audio", "font")
    pixel_size = float(o.get("pixel_size") or 0)
    if fixed_px and not pixel_size > 0:
        raise OpError("Pixel size must be greater than 0.")
    st = {
        # Fixed Pixel Size keeps pictures at their own pixel size (a vector drawn as bitmap is still rasterized at its Scale)
        "scale": (1.0 if fixed_px and cat != "vector" else float(o["scale"])) if (cat != "vector" or bitmap) else 1.0,
        "every": max(1, int(o["every"])),
        "quantize": int(o["quantize"] or 0),
        "svg_scale": float(o["svg_scale"]) * SVG_UNITS_PER_EM,
        "steps": max(2, int(o["steps"])),
        "color": o["color"],
        "smooth": bool(o["smooth"]),
        "bitmap": bitmap,
        "fallback_color": o["fallback_color"],
        # several pictures of different sizes keep their own sizes, so they all end up with the same pixel size
        "no_scale": (bool(o["no_scale"]) or fixed_px) and multi and cat in ("image", "vector") and (cat == "image" or bitmap),
        "fixed_scale": None,
    }
    if st["scale"] <= 0 or float(o["svg_scale"]) <= 0:
        raise OpError("Scale must be greater than 0.")
    if cat == "vector" and not st["color"] and not st["fallback_color"]:
        if any(ct.svg_needs_fallback_color(f, st["bitmap"]) for f in files):
            st["fallback_color"] = [0, 0, 0, 1]  # castletool asks; default to black
    if st["no_scale"]:
        st["fixed_scale"] = ct.fixed_density_scale(files, ico_sizes, cat, st["scale"])
    if fixed_px:
        st["fixed_scale"] = 1.0 / pixel_size               # pixels per Castle unit
    return st


def _frames_in(f: Path, cat: str) -> int:
    """How many frames one file holds (1 for stills, SVG and anything unreadable)."""
    try:
        if cat == "video":
            return max(1, media_av.video_frame_count(f, 1)) if ct.HAS_FFMPEG else 1
        if cat == "animation":
            with ct.Image.open(f) as im:
                return max(1, int(getattr(im, "n_frames", 1) or 1))
    except Exception:
        pass
    return 1


def preview_frame_count(info: dict, opts: dict) -> int:
    """How many different frames the import preview can show with these settings."""
    cat, every = info["category"], max(1, int(float(opts.get("every") or 1)))
    if cat in ("image", "vector") and info["count"] > 1 and opts.get("layout", "frames") == "frames":
        return info["count"]
    if cat in ("animation", "video"):
        return max(1, -(-int(info.get("frames") or 1) // every))
    return 1


def _one_frame_png(f: Path, cat: str, st: dict, ico_size, index: int) -> Optional[tuple]:
    """Decode ONLY frame `index` of `f`, scaled like the import would. Returns (png, w, h)."""
    width, height = ct.scaled_size(ct.visual_native_size(f, cat, ico_size), st["scale"])
    every = max(1, int(st["every"]))
    if cat == "video":
        png = media_av.extract_video_frame(f, index, width, height, every_n=every, smooth=st["smooth"])
    else:
        resample = ct.Image.BILINEAR if st["smooth"] else ct.Image.NEAREST
        with ct.Image.open(f) as img:
            if ico_size:
                img.size = tuple(ico_size)
            n = int(getattr(img, "n_frames", 1) or 1)
            if getattr(img, "is_animated", False):
                img.seek(min(index * every, n - 1))
            buf = io.BytesIO()
            img.convert("RGBA").resize((width, height), resample).save(buf, format="PNG")
            png = buf.getvalue()
    if st["quantize"]:
        png = ct.quantize_frame(png, st["quantize"])
    return png, width, height


@_guard
def preview_media(src: Path, opts: dict, *, log: Log, bg=None, size: int = 220, frame: int = 0) -> Optional[bytes]:
    """PNG of what Import would draw, for the import modal. Renders only frame
    number `frame` (never the whole video / animation), so it stays quick."""
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
        frame = max(0, int(frame))
        by_file = len(files) > 1 and cat in ("image", "vector") and o["layout"] == "frames"
        f = files[min(frame, len(files) - 1)] if by_file else files[0]
        ico = _ico_sizes([f])
        st = _media_state(o, cat, [f], ico, False)
        tasks.check()
        if cat == "vector":
            d2, _summary = ct.render_visual(f, cat, st, ico.get(f))
        else:
            png, w, h = _one_frame_png(f, cat, st, ico.get(f), 0 if by_file else frame)
            d2 = ct.build_drawing2([png], 1.0, w, h, "still", scale=st["fixed_scale"])
        tasks.check()
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
def import_media(bp_path: Path, card_dir: Path, src: Path, opts: dict, *, log: Log,
                 layer: Optional[str] = None) -> list[Path]:
    """Port of do_add_image(). Returns the blueprint files written.

    layer: None = the file becomes the blueprint's whole drawing; lyr.NEW = it is added as a
    new layer; a layer id = it overwrites that layer (other layers are left alone)."""
    o = {**DEFAULT_MEDIA_OPTS, **(opts or {})}
    if layer is not None:
        o["layout"] = "frames"                  # several images become frames of the one layer
    _state["warnings"] = []
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
        if layer is not None and len(files) > 1 and cat not in ("image", "vector"):
            log("warning: a layer holds one file, so only the first one was used.")
            files = files[:1]

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
            if want_audio and not media_av.can_encode_mp3():
                _state["warnings"].append("this device can't encode MP3, so the sound was skipped")
                want_audio = False

        base = copy.deepcopy(actor)
        written: list[Path] = []

        if frames_mode:
            try:
                with tasks.span(0.0, 0.95):
                    if cat == "vector":
                        d2, summary = ct.render_svg_frames_actor(files, st, fps)
                    else:
                        d2, summary = ct.render_frames_actor(files, st, ico_sizes, fps)
            except Exception as e:
                raise OpError(f"Could not import: {e}")
            _put_drawing(actor, d2, layer)
            log(summary)
            tasks.say("Saving blueprint", 0.97)
            write_blueprint(bp_path, actor)
            ct.maybe_delete_placeholder_script(card_dir, bp_path)
            return [bp_path]

        used_selected = False
        for fi, f in enumerate(files):
            tasks.check()
            has_aud = want_audio and f in audio_files
            lo, hi = fi / len(files), (fi + 1) / len(files)
            try:
                with tasks.span(lo, lo + (hi - lo) * (0.5 if has_aud else 0.95)):
                    d2, summary = ct.render_visual(f, cat, st, ico_sizes.get(f))
            except Exception as e:
                log(f"error: {f.name}: {e}")
                continue
            t_path, t_actor, is_sel = ct.next_target(card_dir, bp_path, actor, base, used_selected, f.stem)
            used_selected = True
            if not is_sel:
                ct.strip_audio_rules(t_actor)
            _put_drawing(t_actor, d2, layer if is_sel else None)
            log(summary)
            if has_aud:
                with tasks.span(lo + (hi - lo) * 0.5, hi):
                    if not ct.attach_audio(t_actor, f, is_video=True):
                        log("warning: continuing with silent video.")
                        why = (_state["errors"][-1] if _state["errors"] else "unknown error")
                        why = re.sub(r"^Audio upload failed:\s*", "", why).strip().splitlines()[0][:120]
                        _state["warnings"].append(f"the sound wasn't uploaded ({why})")
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
def import_font(bp_path: Path, card_dir: Path, src: Path, opts: dict, *, log: Log,
                layer: Optional[str] = None) -> list[Path]:
    """Port of do_add_font(). opts: charsets (list of preset names) and/or
    custom (charspec text), bitmap, scale, steps, px, smooth, color.
    layer: see import_media (one font only when a layer is targeted)."""
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
        if layer is not None and len(fonts) > 1:
            log("warning: a layer holds one font, so only the first one was used.")
            fonts = fonts[:1]

        base = copy.deepcopy(actor)
        used_selected = False
        written: list[Path] = []
        for fi, (fp, font) in enumerate(fonts):
            tasks.check()
            lo, hi = fi / len(fonts), (fi + 1) / len(fonts)
            log(f"Rendering {len(codepoints)} glyph(s) from {fp.name}...")
            count, missing, d2 = 0, 0, None
            if bitmap:
                try:
                    with tasks.span(lo, lo + (hi - lo) * 0.95):
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
                for gi, cp in enumerate(codepoints):
                    with tasks.span(lo, lo + (hi - lo) * 0.95):
                        tasks.step(gi, len(codepoints), f"Rendering glyph {gi + 1}/{len(codepoints)}")
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
            _put_drawing(t_actor, d2, layer if is_sel else None)
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


# ── text actors ──────────────────────────────────────────────────────────

# Text.fontName -> CASTLE icon (the "Ab" glyph Castle shows for that font).
# "DMSans" is the font Castle's picker shows with the Overlay icon.
TEXT_FONTS = [
    ("Bore", "font_bore"), ("CourierPrime", "font_courier_prime"), ("Glacier", "font_glacier"),
    ("HelicoCentrica", "font_helico_centrica"), ("DMSans", "font_overlay"), ("Piazzolla", "font_piazzolla"),
    ("Synco", "font_synco"), ("Tektur", "font_tektur"), ("YatraOne", "font_yatra_one"),
]
TEXT_H_ALIGN = ("left", "center", "right")
TEXT_V_ALIGN = ("top", "middle", "bottom")


def text_component(bp: dict) -> Optional[dict]:
    """The blueprint's Text component when it is an active text actor, else None."""
    t = components(bp).get("Text")
    return t if isinstance(t, dict) and not t.get("disabled") else None


def text_color_hex(color) -> str:
    try:
        return "#%02x%02x%02x" % tuple(max(0, min(255, round(float(color[k]) * 255))) for k in "rgb")
    except (TypeError, KeyError, ValueError):
        return "#ffffff"


def set_text_props(bp_path: Path, **props) -> None:
    """Change content / fontName / alignment / verticalAlignment / color of a text actor."""
    bp = load_blueprint(bp_path)
    t = text_component(bp)
    if t is None:
        raise OpError("This blueprint isn't a text actor.")
    if "fontName" in props and props["fontName"] not in {n for n, _ in TEXT_FONTS}:
        raise OpError("Unknown font.")
    if "alignment" in props and props["alignment"] not in TEXT_H_ALIGN:
        raise OpError("Unknown alignment.")
    if "verticalAlignment" in props and props["verticalAlignment"] not in TEXT_V_ALIGN:
        raise OpError("Unknown alignment.")
    if "color" in props:                                  # hex -> {r,g,b,a}, keeping the alpha
        rgba = parse_color(props.pop("color"))
        if not rgba:
            raise OpError("Unknown color.")
        a = (t.get("color") or {}).get("a", 1)
        t["color"] = {"r": rgba[0], "g": rgba[1], "b": rgba[2], "a": a}
    t.update(props)
    write_blueprint(bp_path, bp)


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
        mid = ct.open_midi(midi_path)
    except Exception as e:
        raise OpError(f"Could not read MIDI: {e}")
    with tasks.span(0.0, 0.95):
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
        for fi, f in enumerate(files):
            tasks.check()
            t_path, t_actor, is_sel = ct.next_target(card_dir, bp_path, actor, base, used_selected, f.stem)
            if not is_sel:
                ct.strip_audio_rules(t_actor)
            with tasks.span(fi / len(files), (fi + 1) / len(files)):
                ok = ct.attach_audio(t_actor, f, is_video=False)
            if not ok:
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


# ── Music → Sound / MP3 ──────────────────────────────────────────────────

def has_music(bp: dict) -> bool:
    music = components(bp).get("Music")
    return bool(music) and bool(((music.get("song") or {}).get("tracks")))


def _render_music_wav(music: dict, bpm: float, out: Path) -> float:
    """Render `music` to a mono 16-bit WAV at `out`. Returns seconds. Raises OpError if silent."""
    pcm = live_synth.render_pcm(music, bpm, media_av.SAMPLE_RATE)
    if not len(pcm):
        raise OpError("This music has no notes to render.")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(media_av.SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return len(pcm) / media_av.SAMPLE_RATE


@_guard
def export_music_mp3(bp_path: Path, card_dir: Path, *, log: Log) -> bytes:
    """Render the blueprint's Music to one MP3 (no 30 s limit) and return its bytes."""
    if not media_av.can_encode_mp3():
        raise OpError("This device can't encode MP3.")
    actor = load_blueprint(bp_path)
    music = components(actor).get("Music")
    if not music:
        raise OpError("This blueprint has no music.")
    tmp = Path(tempfile.mkdtemp(prefix="castletool-music-"))
    try:
        tasks.say("Rendering music", 0.0)
        secs = _render_music_wav(music, card_tempo(card_dir), tmp / "music.wav")
        tasks.say("Encoding MP3", 0.5)
        ct.extract_audio_mp3(tmp / "music.wav", tmp / "music.mp3")
        log(f"Music rendered: {secs:.1f}s")
        return (tmp / "music.mp3").read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@_guard
def music_to_sound(bp_path: Path, card_dir: Path, *, log: Log) -> None:
    """Render Music → MP3 → upload to Castle as the blueprint's sound (replacing any existing
    sound rule), then delete the Music component. Nothing changes if the upload fails."""
    if not _state["token"]:
        raise OpError("Sign in first: sound is uploaded to Castle.")
    if not media_av.can_encode_mp3():
        raise OpError("This device can't encode MP3.")
    bp_path, card_dir = Path(bp_path), Path(card_dir)
    actor = load_blueprint(bp_path)
    comps = ensure_components(actor)
    music = comps.get("Music")
    if not music:
        raise OpError("This blueprint has no music.")
    tmp = Path(tempfile.mkdtemp(prefix="castletool-music-"))
    try:
        tasks.say("Rendering music", 0.0)
        with tasks.span(0.0, 0.2):
            secs = _render_music_wav(music, card_tempo(card_dir), tmp / "music.wav")
        log(f"Music rendered: {secs:.1f}s")
        with tasks.span(0.2, 1.0):
            ok = ct.attach_audio(actor, tmp / "music.wav", is_video=False)
        if not ok:
            why = (_state["errors"][-1] if _state["errors"] else "unknown error")
            raise OpError(f"Sound upload failed: {why}")
        comps.pop("Music", None)
        write_blueprint(bp_path, actor)
        log("Music replaced by sound.")
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
