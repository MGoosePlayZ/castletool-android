"""Render a blueprint's Drawing2 component from its actual JSON.

Coordinates (Castle is Y-down, like the screen):
  * vector pathDataList segments: Castle units.           p = [x1, y1, x2, y2]
  * bitmap fillImageBounds:       pixels, same orientation as the vectors;
                                  units = pixels / drawData.fillPixelsPerUnit
  Verified against a Castle-made blueprint: its own thumbnail only matches when
  vectors are drawn without flipping Y, and they line up with the fill image.
Vector strokes are ~0.28 Castle units wide (the brush Castle draws vector lines
with; for scale, one em of a font imported by castletool is 10 units).
"""
from __future__ import annotations

import base64
import io
import math
from typing import Optional

from PIL import Image, ImageDraw

STROKE_UNITS = 0.28
SS = 3  # supersampling for vector strokes


def arc_points(p1, bp, p2) -> list:
    """Circular arc from p1 to p2 that passes through the bend point bp (what you
    drag on a line in Castle's draw tool). Falls back to a straight line when the
    three points are (almost) collinear."""
    (x1, y1), (xb, yb), (x2, y2) = p1, bp, p2
    d = 2 * (x1 * (yb - y2) + xb * (y2 - y1) + x2 * (y1 - yb))
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-9 or abs(d) < 1e-7 * max(1.0, chord * chord):
        return [p1, p2]
    s1, s2, s3 = x1 * x1 + y1 * y1, xb * xb + yb * yb, x2 * x2 + y2 * y2
    ux = (s1 * (yb - y2) + s2 * (y2 - y1) + s3 * (y1 - yb)) / d
    uy = (s1 * (x2 - xb) + s2 * (x1 - x2) + s3 * (xb - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    ab = math.atan2(yb - uy, xb - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    tau = 2 * math.pi
    ccw_b, ccw_2 = (ab - a1) % tau, (a2 - a1) % tau
    sweep = ccw_2 if ccw_b <= ccw_2 else -((a1 - a2) % tau)     # the way round that contains bp
    n = max(6, min(96, int(abs(sweep) * r / 0.15) + 2))
    pts = [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]
    pts[0], pts[-1] = p1, p2                                    # exact end points
    return pts


def segment_points(seg: dict) -> list:
    """Polyline for one pathDataList entry: a straight line, or a circular arc when
    the segment has a bend point `bp`."""
    p = seg.get("p") or []
    if len(p) < 4:
        return []
    bp = seg.get("bp")
    if isinstance(bp, dict) and "x" in bp and "y" in bp:
        return arc_points((p[0], p[1]), (bp["x"], bp["y"]), (p[2], p[3]))
    return [(p[0], p[1]), (p[2], p[3])]


def get_drawing2(bp: dict) -> Optional[dict]:
    return ((bp.get("actorBlueprint") or {}).get("components") or {}).get("Drawing2")


def frame_count(d2: dict) -> int:
    layers = (d2.get("drawData") or {}).get("layers") or []
    return max((len(l.get("frames") or []) for l in layers), default=0)


def drawing_info(d2: dict) -> dict:
    layers = (d2.get("drawData") or {}).get("layers") or []
    bitmap = vector = False
    segs = 0
    for l in layers:
        for f in l.get("frames") or []:
            if f.get("fillPng"):
                bitmap = True
            if f.get("pathDataList"):
                vector = True
                segs += len(f["pathDataList"])
    return {
        "frames": frame_count(d2),
        "fps": d2.get("framesPerSecond") or 4,
        "play_mode": d2.get("playMode") or "still",
        "initial_frame": max(1, int(d2.get("initialFrame") or 1)),
        "bitmap": bitmap,
        "vector": vector,
        "segments": segs,
        "ppu": (d2.get("drawData") or {}).get("fillPixelsPerUnit") or 1,
    }


def _hex_rgb(bg) -> tuple:
    if isinstance(bg, str) and len(bg.lstrip("#")) == 6:
        t = bg.lstrip("#")
        return tuple(int(t[i:i + 2], 16) for i in (0, 2, 4))
    return (0, 0, 0)


def _decode_png(b64: str) -> Optional[Image.Image]:
    try:
        return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA")
    except Exception:
        return None


def resolve_linked(frames: list, index: int) -> int:
    """Looping frames (`isLinked`) look exactly like the frame before them, so follow
    the chain back to the frame that actually holds the drawing."""
    while index > 0 and (frames[index] or {}).get("isLinked"):
        index -= 1
    return index


def _frame_items(d2: dict, index: int):
    """Yield the frame dict of every visible layer at frame `index` (linked frames resolved)."""
    for layer in (d2.get("drawData") or {}).get("layers") or []:
        if layer.get("isVisible") is False:
            continue
        frames = layer.get("frames") or []
        if 0 <= index < len(frames):
            yield frames[resolve_linked(frames, index)]


def _scene_bounds(d2: dict, indices) -> Optional[tuple]:
    """(minX, minY, maxX, maxY) in Castle units (Y down) over the given frames."""
    ppu = float((d2.get("drawData") or {}).get("fillPixelsPerUnit") or 1) or 1.0
    xs, ys = [], []
    half = STROKE_UNITS / 2
    for i in indices:
        for fr in _frame_items(d2, i):
            if fr.get("fillPng"):
                b = fr.get("fillImageBounds") or {}
                xs += [b.get("minX", 0) / ppu, b.get("maxX", 0) / ppu]
                ys += [b.get("minY", 0) / ppu, b.get("maxY", 0) / ppu]
            for seg in fr.get("pathDataList") or []:
                for (px, py) in segment_points(seg):
                    xs += [px - half, px + half]
                    ys += [py - half, py + half]
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def render_frame(d2: dict, index: int = 0, size: int = 128, bg=None,
                 bounds=None, pad: float = 0.06) -> Image.Image:
    """Render one frame into a size×size RGBA image (square, content fitted).
    `bounds` (from _scene_bounds) pins the framing so animation frames line up."""
    bgc = _hex_rgb(bg) + (255,) if bg else (0, 0, 0, 0)
    out = Image.new("RGBA", (size, size), bgc)
    bounds = bounds or _scene_bounds(d2, [index])
    if not bounds:
        return out
    minx, miny, maxx, maxy = bounds
    w, h = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    inner = size * (1 - 2 * pad)
    k = inner / max(w, h)                     # pixels per Castle unit
    ox = (size - w * k) / 2 - minx * k
    oy = (size - h * k) / 2 - miny * k

    ppu = float((d2.get("drawData") or {}).get("fillPixelsPerUnit") or 1) or 1.0
    for fr in _frame_items(d2, index):
        if fr.get("fillPng"):
            img = _decode_png(fr["fillPng"])
            b = fr.get("fillImageBounds") or {}
            if img is not None:
                x0, y0 = b.get("minX", 0) / ppu * k + ox, b.get("minY", 0) / ppu * k + oy
                x1, y1 = b.get("maxX", 0) / ppu * k + ox, b.get("maxY", 0) / ppu * k + oy
                tw, th = max(1, round(x1 - x0)), max(1, round(y1 - y0))
                # crisp for pixel art when enlarging, smooth when shrinking
                res = Image.NEAREST if tw >= img.width else Image.BILINEAR
                _paste_clipped(out, img.resize((tw, th), res), round(x0), round(y0))
        segs = fr.get("pathDataList") or []
        if segs:
            layer = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            width = max(1.0, STROKE_UNITS * k * SS)
            r = width / 2
            for seg in segs:
                pts = segment_points(seg)
                if len(pts) < 2:
                    continue
                c = seg.get("c") or [0, 0, 0, 1]
                col = tuple(max(0, min(255, round(v * 255))) for v in c[:3]) + \
                    (max(0, min(255, round((c[3] if len(c) > 3 else 1) * 255))),)
                xy = [((x * k + ox) * SS, (y * k + oy) * SS) for x, y in pts]
                dr.line(xy, fill=col, width=round(width), joint="curve")
                if width > 2:  # round caps
                    for (x, y) in (xy[0], xy[-1]):
                        dr.ellipse([x - r, y - r, x + r, y + r], fill=col)
            layer = layer.resize((size, size), Image.LANCZOS)
            out.alpha_composite(layer)
    return out


def _paste_clipped(dst: Image.Image, src: Image.Image, x: int, y: int) -> None:
    layer = Image.new("RGBA", dst.size, (0, 0, 0, 0))
    layer.paste(src, (x, y))
    dst.alpha_composite(layer)


def to_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_png(bp_or_d2: dict, size: int = 128, bg=None, frame: Optional[int] = None) -> Optional[bytes]:
    """Thumbnail of a blueprint (or Drawing2 dict) at its initial frame."""
    d2 = get_drawing2(bp_or_d2) if "actorBlueprint" in bp_or_d2 else bp_or_d2
    if not d2:
        return None
    idx = (max(1, int(d2.get("initialFrame") or 1)) - 1) if frame is None else frame
    n = frame_count(d2)
    if n == 0:
        return None
    idx = min(idx, n - 1)
    return to_png(render_frame(d2, idx, size, bg))


def render_animation(d2: dict, size: int = 256, bg=None, max_frames: int = 240, cancel=None) -> list[bytes]:
    """All frames (capped), framed identically using the union bounds. `cancel` is polled
    between frames: when it returns True the job stops and returns []."""
    n = min(frame_count(d2), max_frames)
    if n == 0:
        return []
    bounds = _scene_bounds(d2, range(n))
    out = []
    for i in range(n):
        if cancel and cancel():
            return []
        out.append(to_png(render_frame(d2, i, size, bg, bounds=bounds)))
    return out


class ExportError(Exception):
    """Something the person should read."""


VECTOR_PX_PER_UNIT = 32       # vector-only layers have no pixel grid of their own
MAX_EXPORT_PIXELS = 64_000_000


def export_layer(d2: dict, layer_id: str, cancel=None) -> tuple[bytes, int]:
    """One layer as a PNG, or as an APNG (looping at the drawing's FPS) when the drawing has
    several frames. Bitmap pixels are kept 1:1; the canvas is the union of every frame so an
    APNG lines up. Returns (png_bytes, frame_count)."""
    draw = d2.get("drawData") or {}
    layer = next((l for l in draw.get("layers") or [] if l.get("id") == layer_id), None)
    if layer is None:
        raise ExportError("That layer no longer exists.")
    n = len(draw.get("framesBounds") or [])
    for l in draw.get("layers") or []:
        n = max(n, len(l.get("frames") or []))
    n = max(1, n)
    ppu = float(draw.get("fillPixelsPerUnit") or 1) or 1.0
    own = layer.get("frames") or []
    frames = []
    for i in range(n):
        j = min(i, len(own) - 1)
        frames.append(own[resolve_linked(own, j)] or {} if j >= 0 else {})
    has_bitmap = any(f.get("fillPng") for f in frames)
    k = ppu if has_bitmap else float(VECTOR_PX_PER_UNIT)          # output pixels per Castle unit
    half = STROKE_UNITS * k / 2
    xs, ys = [], []
    for f in frames:
        if f.get("fillPng"):
            b = f.get("fillImageBounds") or {}
            xs += [b.get("minX", 0) * k / ppu, b.get("maxX", 0) * k / ppu]
            ys += [b.get("minY", 0) * k / ppu, b.get("maxY", 0) * k / ppu]
        for seg in f.get("pathDataList") or []:
            for (px, py) in segment_points(seg):
                xs += [px * k - half, px * k + half]
                ys += [py * k - half, py * k + half]
    if not xs or max(xs) <= min(xs) or max(ys) <= min(ys):
        raise ExportError("This layer is empty. Nothing to export.")
    ox, oy = math.floor(min(xs)), math.floor(min(ys))
    w, h = max(1, math.ceil(max(xs)) - ox), max(1, math.ceil(max(ys)) - oy)
    if w * h > MAX_EXPORT_PIXELS:
        raise ExportError(f"This layer is too large to export ({w}\u00d7{h}px).")
    out = []
    for f in frames:
        if cancel and cancel():
            raise ExportError("Cancelled.")
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if f.get("fillPng"):
            src = _decode_png(f["fillPng"])
            b = f.get("fillImageBounds") or {}
            if src is not None:
                bx, by = (b.get("minX", 0) * k / ppu) - ox, (b.get("minY", 0) * k / ppu) - oy
                bw = max(1, round((b.get("maxX", 0) - b.get("minX", 0)) * k / ppu))
                bh = max(1, round((b.get("maxY", 0) - b.get("minY", 0)) * k / ppu))
                _paste_clipped(img, src.resize((bw, bh), Image.NEAREST), round(bx), round(by))
        segs = f.get("pathDataList") or []
        if segs:
            layer_img = Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer_img)
            width = max(1.0, STROKE_UNITS * k * SS)
            r = width / 2
            for seg in segs:
                pts = segment_points(seg)
                if len(pts) < 2:
                    continue
                c = seg.get("c") or [0, 0, 0, 1]
                col = tuple(max(0, min(255, round(v * 255))) for v in c[:3]) + \
                    (max(0, min(255, round((c[3] if len(c) > 3 else 1) * 255))),)
                xy = [((x * k - ox) * SS, (y * k - oy) * SS) for x, y in pts]
                dr.line(xy, fill=col, width=round(width), joint="curve")
                for (x, y) in (xy[0], xy[-1]):
                    dr.ellipse([x - r, y - r, x + r, y + r], fill=col)
            img.alpha_composite(layer_img.resize((w, h), Image.LANCZOS))
        out.append(img)
    buf = io.BytesIO()
    if len(out) == 1:
        out[0].save(buf, format="PNG")
    else:
        fps = float(d2.get("framesPerSecond") or 4) or 4.0
        out[0].save(buf, format="PNG", save_all=True, append_images=out[1:], loop=0,
                    duration=max(1, round(1000 / max(0.01, min(60.0, fps)))), disposal=0, blend=0)
    return buf.getvalue(), len(out)


def b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")
