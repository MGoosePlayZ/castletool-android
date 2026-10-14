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


def _frame_items(d2: dict, index: int):
    """Yield (layer_kind, frame_dict) for every visible layer at frame `index`."""
    for layer in (d2.get("drawData") or {}).get("layers") or []:
        if layer.get("isVisible") is False:
            continue
        frames = layer.get("frames") or []
        if 0 <= index < len(frames):
            yield frames[index]


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


def render_animation(d2: dict, size: int = 256, bg=None, max_frames: int = 240) -> list[bytes]:
    """All frames (capped), framed identically using the union bounds."""
    n = min(frame_count(d2), max_frames)
    if n == 0:
        return []
    bounds = _scene_bounds(d2, range(n))
    return [to_png(render_frame(d2, i, size, bg, bounds=bounds)) for i in range(n)]


def b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")
