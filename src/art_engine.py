"""Art editor engine (no UI): layers x frames of vector "shapes" or raster "paint", Castle Drawing2 in and out.

Units: canvas pixels, Y down. One canvas pixel = 1 / fillPixelsPerUnit Castle units; the canvas centre is the
Castle origin. A frame's data object is SHARED between linked frames (same object = linked).

  shapes frame  lines  [{a, b, m, q, c}]  a/b end points, m = arc midpoint (or None), q = quarter-arc kind, c = '#rrggbb'
                fills  [{x, y, c, px, ok}]  buckets: remember their pixels, re-flood when lines change
  paint frame   px     (H, W, 4) uint8 RGBA. Arrays are never changed in place once a snapshot may hold them: call
                `PaintData.own()` before painting (copy-on-write), so undo snapshots are cheap.
"""
from __future__ import annotations

import base64
import io
import math
import random
import re
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

LW = 7                  # stroke width of shape lines, px
HIT = 6                 # pick distance for shape lines, px
SS = 3                  # supersampling of strokes


# ── colors ─────────────────────────────────────────────────────────────────

def hex_rgb(h: str) -> tuple:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    h = (h + "000000")[:6]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_hex(r, g, b) -> str:
    return "#%02x%02x%02x" % (int(r), int(g), int(b))


def c2h(c) -> str:
    c = list(c or [1, 1, 1, 1])[:3]
    return rgb_hex(*[round(max(0.0, min(1.0, v)) * 255) for v in c])


# ── geometry ───────────────────────────────────────────────────────────────

def sag_k(p1, bp, p2):
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return None
    n = (-dy / L, dx / L)
    m = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
    h = (bp[0] - m[0]) * n[0] + (bp[1] - m[1]) * n[1]
    h = math.copysign(min(abs(h), L / 2), h)
    a = abs(h)
    if a < 1e-6:
        return None
    R = (L * L / 4 + a * a) / (2 * a)
    sg = 1 if h > 0 else -1
    c = (m[0] + n[0] * sg * (a - R), m[1] + n[1] * sg * (a - R))
    ap = (m[0] + n[0] * h, m[1] + n[1] * h)
    ccw = (p1[0] - c[0]) * (ap[1] - c[1]) - (p1[1] - c[1]) * (ap[0] - c[0]) > 0
    return c, R, ccw


def norm_m(a, m, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    n = (-dy / L, dx / L)
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    h = (m[0] - mx) * n[0] + (m[1] - my) * n[1]
    h = math.copysign(min(abs(h), L / 2), h)
    if abs(h) < 1:
        return None
    return (mx + n[0] * h, my + n[1] * h)


def quarter(a, b, k):
    c1, c2 = (a[0], b[1]), (b[0], a[1])
    cen = (c1 if c1[1] >= c2[1] else c2) if k == 2 else (c1 if c1[1] <= c2[1] else c2)
    ux, uy, vx, vy = a[0] - cen[0], a[1] - cen[1], b[0] - cen[0], b[1] - cen[1]
    out = []
    for i in range(25):
        f = math.pi / 2 * i / 24
        c, s = math.cos(f), math.sin(f)
        out.append((cen[0] + ux * c + vx * s, cen[1] + uy * c + vy * s))
    return out


def pts(l: dict, n: Optional[int] = None) -> list:
    if l.get("q"):
        return quarter(l["a"], l["b"], l["q"])
    A = sag_k(l["a"], l["m"], l["b"]) if l.get("m") else None
    if not A:
        return [tuple(l["a"]), tuple(l["b"])]
    c, R, ccw = A
    T = math.tau
    md = lambda v: ((v % T) + T) % T
    a1 = math.atan2(l["a"][1] - c[1], l["a"][0] - c[0])
    a2 = math.atan2(l["b"][1] - c[1], l["b"][0] - c[0])
    sw = md(a2 - a1) if ccw else -md(a1 - a2)
    n = n or max(8, min(96, math.ceil(abs(sw) * R / 3)))
    o = [(c[0] + R * math.cos(a1 + sw * i / n), c[1] + R * math.sin(a1 + sw * i / n)) for i in range(n + 1)]
    o[0], o[n] = tuple(l["a"]), tuple(l["b"])
    return o


def samp(l: dict) -> list:
    return pts(l, 24)


def seg_d(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L if L else 0
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def dist(p, l) -> float:
    s = samp(l)
    return min(seg_d(p, s[i], s[i + 1]) for i in range(len(s) - 1))


def mid_point(l: dict):
    if l.get("q"):
        return pts(l)[12]
    m = norm_m(l["a"], l["m"], l["b"]) if l.get("m") else None
    return m or ((l["a"][0] + l["b"][0]) / 2, (l["a"][1] + l["b"][1]) / 2)


# ── data ───────────────────────────────────────────────────────────────────

class ShapeData:
    kind = "shapes"

    def __init__(self, lines=None, fills=None):
        self.lines: list = lines if lines is not None else []
        self.fills: list = fills if fills is not None else []
        self.dirty = True
        self.fc: Optional[np.ndarray] = None            # filled buckets as RGBA (H, W, 4)
        self.cache: Optional[np.ndarray] = None         # fills + strokes, rendered

    def copy(self) -> "ShapeData":
        return ShapeData([{**l, "a": tuple(l["a"]), "b": tuple(l["b"]), "m": tuple(l["m"]) if l.get("m") else None}
                          for l in self.lines], [dict(f) for f in self.fills])

    def touch(self):
        self.dirty = True
        self.cache = None


class PaintData:
    kind = "paint"

    def __init__(self, W: int, H: int, px: Optional[np.ndarray] = None):
        self.px = px if px is not None else np.zeros((H, W, 4), np.uint8)
        self.shared = False                             # a snapshot may hold px: copy before writing
        self.cache = None

    def own(self) -> np.ndarray:
        if self.shared:
            self.px = self.px.copy()
            self.shared = False
        self.cache = None
        return self.px

    def copy(self) -> "PaintData":
        n = PaintData(0, 0, self.px)
        n.shared = self.shared = True
        return n


def new_data(kind: str, W: int, H: int):
    return ShapeData() if kind == "shapes" else PaintData(W, H)


class Layer:
    def __init__(self, name: str, kind: str, fr: list, vis: bool = True, lid: Optional[str] = None):
        self.name, self.type, self.fr, self.vis, self.id = name, kind, fr, vis, lid


class Doc:
    def __init__(self, W: int = 288, H: int = 288, ppu: float = 25.6):
        self.W, self.H, self.ppu = W, H, ppu
        self.layers: list[Layer] = []
        self.F = 1
        self.base: dict = {}                            # drawData keys we don't edit (kept as they were)
        self.pending: list = []                         # [(shape_data, fill PNG b64, bounds)] decoded later

    def linked(self, l: Layer, i: int) -> bool:
        return i > 0 and l.fr[i] is l.fr[i - 1]

    def add_layer(self, kind: str, name: Optional[str] = None) -> Layer:
        nums = [int(m.group(1)) for x in self.layers if (m := re.search(r"(\d+)$", x.name))]
        n = max([0] + nums) + 1
        l = Layer(name or (("Shapes " if kind == "shapes" else "Paint ") + str(n)), kind,
                  [new_data(kind, self.W, self.H) for _ in range(self.F)])
        self.layers.append(l)
        return l

    # undo snapshots: structure copied, paint arrays shared (copy-on-write), sharing between linked frames kept
    def snapshot(self):
        memo: dict = {}

        def cp(d):
            if id(d) not in memo:
                memo[id(d)] = d.copy()
            return memo[id(d)]
        return {"F": self.F, "layers": [(l.name, l.type, l.vis, l.id, [cp(d) for d in l.fr]) for l in self.layers]}

    def restore(self, snap):
        memo: dict = {}

        def cp(d):
            if id(d) not in memo:
                memo[id(d)] = d.copy()
            return memo[id(d)]
        self.F = snap["F"]
        self.layers = [Layer(n, t, [cp(d) for d in fr], v, i) for n, t, v, i, fr in snap["layers"]]
        for l in self.layers:
            for d in l.fr:
                if isinstance(d, ShapeData):
                    d.touch()


# ── masks and flood fill ──────────────────────────────────────────────────

def _poly_mask(lines: list, W: int, H: int, width: float, ss: int = SS) -> np.ndarray:
    """Coverage (0..255) of the given lines drawn `width` px wide with round caps."""
    im = Image.new("L", (W * ss, H * ss), 0)
    d = ImageDraw.Draw(im)
    w = max(1, round(width * ss))
    r = w / 2
    for l in lines:
        p = [(x * ss, y * ss) for x, y in pts(l)]
        d.line(p, fill=255, width=w, joint="curve")
        for x, y in (p[0], p[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return np.asarray(im.resize((W, H), Image.BOX))


def flood(free: np.ndarray, sy: int, sx: int, seen: np.ndarray):
    """4-connected region of True pixels of `free` containing (sy, sx), skipping `seen` ones.
    Returns (flat indices, touches_border). Scanline fill: row runs found with numpy."""
    H, W = free.shape
    ok = free & ~seen
    if not ok[sy, sx]:
        return np.empty(0, np.int64), False
    runs, stack, leak = [], [(sy, sx)], False
    while stack:
        y, x = stack.pop()
        if seen[y, x] or not free[y, x]:
            continue
        row = free[y]
        bad = np.flatnonzero(~row[:x])
        x0 = int(bad[-1]) + 1 if len(bad) else 0
        bad = np.flatnonzero(~row[x:])
        x1 = x + int(bad[0]) if len(bad) else W
        seen[y, x0:x1] = True
        runs.append((y, x0, x1))
        if y in (0, H - 1) or x0 == 0 or x1 == W:
            leak = True
        for yy in (y - 1, y + 1):
            if 0 <= yy < H:
                cand = free[yy, x0:x1] & ~seen[yy, x0:x1]
                if cand.any():
                    d = np.diff(np.concatenate(([0], cand.view(np.int8), [0])))
                    for s in np.flatnonzero(d == 1):
                        stack.append((yy, x0 + int(s)))
    if not runs:
        return np.empty(0, np.int64), False
    idx = np.concatenate([y * W + np.arange(a, b) for y, a, b in runs])
    return idx, leak


# ── shape fills (buckets) ─────────────────────────────────────────────────

def compute_fills(d: ShapeData, W: int, H: int) -> None:
    """Re-flood every bucket from the pixels it remembers: a new line splits a fill in two, an opening
    (a leak to the border) makes it dormant. Walls are the thin centre line of every stroke."""
    if not d.dirty and d.fc is not None:
        return
    d.dirty = False
    wall = _poly_mask(d.lines, W, H, LW * 0.5) > 20
    free = ~wall
    out = np.zeros((H, W, 4), np.uint8)
    res = []
    for s in d.fills:
        src = s.get("px")
        if src is None:
            x, y = int(s["x"]), int(s["y"])
            if x < 0 or y < 0 or x >= W or y >= H:
                src = []
            elif free[y, x]:
                src = [y * W + x]
            else:
                src = []
                for r in range(1, 5):
                    hit = [(y + dy) * W + (x + dx) for dy in range(-r, r + 1) for dx in range(-r, r + 1)
                           if 0 <= x + dx < W and 0 <= y + dy < H and free[y + dy, x + dx]]
                    if hit:
                        src = hit[:1]
                        break
        seen = np.zeros((H, W), bool)
        comps, leak = [], False
        for i0 in np.asarray(src, np.int64).tolist():
            y0, x0 = divmod(i0, W)
            if seen[y0, x0] or wall[y0, x0]:
                continue
            idx, lk = flood(free, y0, x0, seen)
            if lk:
                leak = True
                break
            if len(idx):
                comps.append(idx)
        if leak or not comps:
            res.append({**s, "ok": 0})
            continue
        col = hex_rgb(s["c"])
        for idx in comps:
            m = np.zeros(H * W, bool)
            m[idx] = True
            m = m.reshape(H, W)
            grow = m.copy()                                     # one pixel into the wall: no seam under the line
            grow[1:] |= m[:-1]; grow[:-1] |= m[1:]; grow[:, 1:] |= m[:, :-1]; grow[:, :-1] |= m[:, 1:]
            grow &= wall | m
            out[grow] = (*col, 255)
            res.append({"x": int(idx[0] % W), "y": int(idx[0] // W), "c": s["c"], "px": idx, "ok": 1})
    d.fills = res
    d.fc = out
    d.cache = None


def shape_fill_at(d: ShapeData, p, W: int):
    i = round(p[1]) * W + round(p[0])
    for f in d.fills:
        if f.get("ok") and f.get("px") is not None and (f.get("set") or f.__setitem__("set", set(map(int, f["px"])))
                                                         or f["set"]) and i in f["set"]:
            return f
    return None


def shape_bucket(d: ShapeData, p, color: str, W: int, H: int):
    h = next((l for l in d.lines if dist(p, l) < HIT), None)
    if h:
        h["c"] = color
        d.touch()
        return
    x, y = round(p[0]), round(p[1])
    if x < 0 or y < 0 or x >= W or y >= H:
        return
    d.fills.append({"x": x, "y": y, "c": color, "px": None})
    d.touch()
    compute_fills(d, W, H)
    last = d.fills[-1]
    if not last.get("ok"):
        d.fills.pop()
    else:
        i = y * W + x
        d.fills = [f for f in d.fills if f is last or not (f.get("px") is not None and i in set(map(int, f["px"])))]
    d.touch()


def color_at(layer: Layer, d, p, W: int, H: int) -> Optional[str]:
    if layer.type == "shapes":
        h = next((n for n in d.lines if dist(p, n) < HIT), None)
        if h:
            return h["c"]
        compute_fills(d, W, H)
        f = shape_fill_at(d, p, W)
        return f["c"] if f else None
    x, y = math.floor(p[0]), math.floor(p[1])
    if x < 0 or y < 0 or x >= W or y >= H:
        return None
    q = d.px[y, x]
    return rgb_hex(*q[:3]) if q[3] else None


def swap_all(layer: Layer, d, p, color: str, W: int, H: int):
    t = color_at(layer, d, p, W, H)
    if not t or t == color:
        return
    if layer.type == "shapes":
        for l in d.lines:
            if l["c"] == t:
                l["c"] = color
        for f in d.fills:
            if f["c"] == t:
                f["c"] = color
        d.touch()
    else:
        px = d.own()
        tc = np.array(hex_rgb(t), np.int16)
        m = (px[..., 3] > 0) & (np.abs(px[..., :3].astype(np.int16) - tc).sum(axis=2) < 8)
        px[m, :3] = hex_rgb(color)


# ── shape builders and editing ────────────────────────────────────────────

def sq(raw: list, color: str, arc: bool = False) -> list:
    idx, acc = [0], 0.0
    for i in range(1, len(raw)):
        acc += math.hypot(raw[i][0] - raw[i - 1][0], raw[i][1] - raw[i - 1][1])
        if acc >= 14:
            idx.append(i)
            acc = 0.0
    if idx[-1] != len(raw) - 1:
        idx.append(len(raw) - 1)
    out = []
    for k in range(1, len(idx)):
        i, j = idx[k - 1], idx[k]
        a, b = raw[i], raw[j]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < .5:
            continue
        m = norm_m(a, raw[(i + j) >> 1], b) if arc and j - i > 1 else None
        out.append({"a": tuple(a), "b": tuple(b), "m": m, "q": 0, "c": color})
    return out


def make_shape(kind: str, p0, raw: list, color: str, arc: bool = False) -> list:
    a, b = p0, raw[-1]
    L = lambda x, y, m=None: {"a": tuple(x), "b": tuple(y), "m": m, "q": 0, "c": color}
    if kind == "squiggle":
        return sq(raw, color, arc)
    if kind == "line":
        return [L(a, b)]
    if kind == "square":
        q = [a, (b[0], a[1]), b, (a[0], b[1])]
        return [L(v, q[(i + 1) % 4]) for i, v in enumerate(q)]
    if kind == "triangle":
        q = [a, (a[0], b[1]), b]
        return [L(v, q[(i + 1) % 3]) for i, v in enumerate(q)]
    ln = math.hypot(b[0] - a[0], b[1] - a[1])
    if ln < 2:
        return []
    r = ln / 2
    cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    ux, uy = (b[0] - a[0]) / ln, (b[1] - a[1]) / ln
    nx, ny = -uy, ux
    pt = lambda t: (cx + r * (math.cos(t) * ux + math.sin(t) * nx), cy + r * (math.cos(t) * uy + math.sin(t) * ny))
    return [L(pt(math.pi + i * math.pi / 2), pt(math.pi + (i + 1) * math.pi / 2),
              pt(math.pi + i * math.pi / 2 + math.pi / 4)) for i in range(4)]


def erase_shapes(d: ShapeData, p, r: float, cut: bool) -> bool:
    ch, out = False, []
    for l in d.lines:
        if cut:
            if dist(p, l) < r:
                ch = True
            else:
                out.append(l)
            continue
        if l.get("q") or (l.get("m") and sag_k(l["a"], l["m"], l["b"])):
            if any(math.hypot(q[0] - p[0], q[1] - p[1]) < r for q in samp(l)):
                ch = True
            else:
                out.append(l)
            continue
        n = max(1, math.ceil(math.hypot(l["b"][0] - l["a"][0], l["b"][1] - l["a"][1])))
        run, hit = None, False

        def flush():
            nonlocal run
            if run and math.hypot(run[2] - run[0], run[3] - run[1]) >= 1:
                out.append({"a": (run[0], run[1]), "b": (run[2], run[3]), "m": None, "q": 0, "c": l["c"]})
            run = None
        for i in range(n + 1):
            t = i / n
            x, y = l["a"][0] + (l["b"][0] - l["a"][0]) * t, l["a"][1] + (l["b"][1] - l["a"][1]) * t
            if math.hypot(x - p[0], y - p[1]) < r:
                hit = True
                flush()
            elif run is None:
                run = [x, y, x, y]
            else:
                run[2], run[3] = x, y
        if not hit:
            out.append(l)
        else:
            ch = True
            flush()
    if ch:
        d.lines = out
        d.touch()
    return ch


def move_shapes(d: ShapeData, dx: int, dy: int):
    for l in d.lines:
        for k in ("a", "b", "m"):
            if l.get(k):
                l[k] = (l[k][0] + dx, l[k][1] + dy)
    for f in d.fills:
        f["x"] += dx
        f["y"] += dy
    d.touch()


def shift_fill_pixels(d: ShapeData, ox: int, oy: int, W: int, H: int):
    for f in d.fills:
        if f.get("px") is None:
            continue
        px = np.asarray(f["px"], np.int64)
        x, y = px % W + ox, px // W + oy
        keep = (x >= 0) & (y >= 0) & (x < W) & (y < H)
        f["px"] = (y * W + x)[keep]
        f["set"] = None
    d.touch()


def move_handle(d: ShapeData, old, new):
    dx, dy = new[0] - old[0], new[1] - old[1]
    for l in d.lines:
        for k in ("a", "b"):
            if math.hypot(l[k][0] - old[0], l[k][1] - old[1]) < .5:
                l[k] = (new[0], new[1])
                if l.get("m"):
                    l["m"] = norm_m(l["a"], (l["m"][0] + dx / 2, l["m"][1] + dy / 2), l["b"])
    d.touch()


def pick_handle(d: ShapeData, mode: str, p, tol: float):
    best, bd = None, tol
    for l in d.lines:
        for q in ([l["a"], l["b"]] if mode == "modify" else [mid_point(l)]):
            h = math.hypot(q[0] - p[0], q[1] - p[1])
            if h < bd:
                bd, best = h, (q if mode == "modify" else l)
    return best


# ── paint tools ───────────────────────────────────────────────────────────

_DISC: dict = {}


def disc(r: float) -> np.ndarray:
    """Hard-edged stamp (no anti-aliasing): integer pixel rows, like the prototype's dot()."""
    key = round(r * 2)
    if key not in _DISC:
        R = math.floor(r)
        m = np.zeros((2 * R + 1, 2 * R + 1), bool)
        for dy in range(-R, R + 1):
            w = math.floor(math.sqrt(max(0.0, r * r - dy * dy + .25)))
            m[dy + R, R - w:R + w + 1] = True
        _DISC[key] = m
    return _DISC[key]


def stamp(px: np.ndarray, x: int, y: int, r: float, color, erase: bool):
    m = disc(r)
    R = m.shape[0] // 2
    H, W = px.shape[:2]
    x0, y0, x1, y1 = x - R, y - R, x + R + 1, y + R + 1
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    if cx0 >= cx1 or cy0 >= cy1:
        return
    sub = m[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    reg = px[cy0:cy1, cx0:cx1]
    if erase:
        reg[sub] = 0
    else:
        reg[sub] = (*color, 255)


def paint_line(d: PaintData, a, b, color: str, erase: bool, r: float):
    px = d.px
    col = hex_rgb(color)
    ax, ay, bx, by = round(a[0]), round(a[1]), round(b[0]), round(b[1])
    n = max(abs(bx - ax), abs(by - ay), 1)
    for i in range(n + 1):
        stamp(px, round(ax + (bx - ax) * i / n), round(ay + (by - ay) * i / n), r, col, erase)
    d.cache = None


def spray(d: PaintData, p, color: str, size: int):
    px, col = d.px, hex_rgb(color)
    H, W = px.shape[:2]
    for _ in range(size + 6):
        q, t = math.sqrt(random.random()) * size, random.random() * 6.283
        x, y = round(p[0] + math.cos(t) * q), round(p[1] + math.sin(t) * q)
        if 0 <= x < W and 0 <= y < H:
            px[y, x] = (*col, 255)
    d.cache = None


def paint_fill(d: PaintData, p, color: Optional[str]):
    """Flood fill of the pixels like the one clicked (colour None = erase the region)."""
    px = d.px
    H, W = px.shape[:2]
    sx, sy = math.floor(p[0]), math.floor(p[1])
    if sx < 0 or sy < 0 or sx >= W or sy >= H:
        return
    t = px[sy, sx].astype(np.int16)
    col = hex_rgb(color) if color else (0, 0, 0)
    al = 255 if color else 0
    if color and t[3] == 255 and tuple(t[:3]) == col:
        return
    if not color and not t[3]:
        return
    eq = np.abs(px.astype(np.int16) - t).sum(axis=2) < 8
    idx, _ = flood(eq, sy, sx, np.zeros((H, W), bool))
    d.own()
    flat = d.px.reshape(-1, 4)
    flat[idx] = (*col, al)


def transform(doc: Doc, d, kind: str):
    """rot (90 clockwise) / fh / fv of one frame's data about the canvas centre."""
    W, H = doc.W, doc.H
    f = {"rot": lambda x, y: (W / 2 - (y - H / 2), H / 2 + (x - W / 2)),
         "fh": lambda x, y: (W - x, y), "fv": lambda x, y: (x, H - y)}[kind]
    if isinstance(d, ShapeData):
        for l in d.lines:
            if l.get("q"):
                l["m"], l["q"] = pts(l)[12], 0
        for l in d.lines:
            l["a"], l["b"] = f(*l["a"]), f(*l["b"])
            l["m"] = f(*l["m"]) if l.get("m") else None
        pf = lambda x, y: tuple(math.floor(v) for v in f(x + .5, y + .5))
        for s in d.fills:
            s["x"], s["y"] = pf(s["x"], s["y"])
            if s.get("px") is not None:
                px = np.asarray(s["px"], np.int64)
                pts_ = [pf(int(i % W), int(i // W)) for i in px]
                s["px"] = np.array([b * W + a for a, b in pts_ if 0 <= a < W and 0 <= b < H], np.int64)
                s["set"] = None
        d.touch()
    else:
        px = d.own()
        if kind == "rot":
            px[:] = np.rot90(px.copy(), -1) if W == H else px
        elif kind == "fh":
            px[:] = px[:, ::-1].copy()
        else:
            px[:] = px[::-1].copy()


# ── rendering ─────────────────────────────────────────────────────────────

def _strokes(lines: list, W: int, H: int) -> np.ndarray:
    """RGBA of the shape lines, anti-aliased (premultiplied while shrinking, so edges stay clean)."""
    if not lines:
        return np.zeros((H, W, 4), np.uint8)
    im = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    w = LW * SS
    r = w / 2
    for l in lines:
        col = (*hex_rgb(l["c"]), 255)
        p = [(x * SS, y * SS) for x, y in pts(l)]
        d.line(p, fill=col, width=w, joint="curve")
        for x, y in (p[0], p[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    return np.asarray(im.convert("RGBa").resize((W, H), Image.BOX).convert("RGBA"))


def over(dst: np.ndarray, src: np.ndarray) -> None:
    """Alpha-composite src onto dst (both RGBA uint8, same shape), in place."""
    a = src[..., 3:4].astype(np.float32) / 255
    if not a.any():
        return
    da = dst[..., 3:4].astype(np.float32) / 255
    oa = a + da * (1 - a)
    rgb = (src[..., :3] * a + dst[..., :3] * da * (1 - a)) / np.maximum(oa, 1e-6)
    dst[..., :3] = np.round(rgb).astype(np.uint8)
    dst[..., 3:4] = np.round(oa * 255).astype(np.uint8)


def frame_rgba(doc: Doc, layer: Layer, d, extra_lines: Optional[list] = None) -> np.ndarray:
    W, H = doc.W, doc.H
    if isinstance(d, PaintData):
        return d.px
    compute_fills(d, W, H)
    if d.cache is None:
        out = d.fc.copy() if d.fc is not None else np.zeros((H, W, 4), np.uint8)
        over(out, _strokes(d.lines, W, H))
        d.cache = out
    if extra_lines:
        out = d.cache.copy()
        over(out, _strokes(extra_lines, W, H))
        return out
    return d.cache


_CHECK: dict = {}


def checker(W: int, H: int) -> np.ndarray:
    if (W, H) not in _CHECK:
        yy, xx = np.mgrid[0:H, 0:W]
        dark = ((xx // 16) + (yy // 16)) % 2 == 0
        a = np.empty((H, W, 4), np.uint8)
        a[..., :3] = np.where(dark[..., None], 0x57, 0x7a)
        a[..., 3] = 255
        _CHECK.clear()
        _CHECK[(W, H)] = a
    return _CHECK[(W, H)]


def render_doc(doc: Doc, cf: int, sel: int, tmp: Optional[list] = None, handles: Optional[tuple] = None,
               zoom: float = 1.0) -> np.ndarray:
    """The canvas as RGBA: checkerboard, visible layers of frame `cf` bottom-up, the stroke being drawn,
    and (grab tool) the handles of the selected layer."""
    out = checker(doc.W, doc.H).copy()
    for i, l in enumerate(doc.layers):
        if l.vis:
            over(out, frame_rgba(doc, l, l.fr[cf], tmp if (i == sel and tmp) else None))
    if handles and 0 <= sel < len(doc.layers):
        mode = handles[0]
        d = doc.layers[sel].fr[cf]
        if isinstance(d, ShapeData):
            h = max(2, round(5 / zoom))
            im = Image.fromarray(out, "RGBA")
            dr = ImageDraw.Draw(im)
            for l in d.lines:
                for q in ([l["a"], l["b"]] if mode == "modify" else [mid_point(l)]):
                    box = [q[0] - h, q[1] - h, q[0] + h, q[1] + h]
                    if mode == "arc":
                        dr.ellipse(box, fill="#ffffff", outline="#000000")
                    else:
                        dr.rectangle(box, fill="#ffffff", outline="#000000")
            out = np.asarray(im)
    return out


def to_png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def bbox(layer: Layer, d, doc: Doc):
    if isinstance(d, ShapeData):
        xs, ys = [], []
        for n in d.lines:
            for q in samp(n):
                xs.append(q[0]); ys.append(q[1])
        compute_fills(d, doc.W, doc.H)
        a = d.fc if d.fc is not None else None
        if a is not None and a[..., 3].any():
            yy, xx = np.nonzero(a[..., 3])
            xs += [int(xx.min()), int(xx.max())]; ys += [int(yy.min()), int(yy.max())]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else None
    a = d.px[..., 3]
    if not a.any():
        return None
    yy, xx = np.nonzero(a)
    return int(xx.min()), int(yy.min()), int(xx.max()), int(yy.max())


def thumb(doc: Doc, layer: Layer, d, N: int = 76, selected: bool = False) -> bytes:
    """Frame thumbnail: zoomed to what is drawn, black background (white when selected)."""
    bg = (255, 255, 255, 255) if selected else (0, 0, 0, 255)
    out = Image.new("RGBA", (N, N), bg)
    bb = bbox(layer, d, doc)
    if bb:
        src = Image.fromarray(frame_rgba(doc, layer, d), "RGBA")
        w, h = max(bb[2] - bb[0], 4), max(bb[3] - bb[1], 4)
        s = min((N - 12) / max(w, h), 8)
        tw, th = max(1, round(doc.W * s)), max(1, round(doc.H * s))
        big = src.resize((tw, th), Image.NEAREST)
        ox, oy = round(N / 2 - (bb[0] + bb[2]) / 2 * s), round(N / 2 - (bb[1] + bb[3]) / 2 * s)
        out.alpha_composite(big, (ox, oy)) if ox >= 0 and oy >= 0 else out.paste(
            big.crop((max(0, -ox), max(0, -oy), tw, th)), (max(0, ox), max(0, oy)), big.crop((max(0, -ox), max(0, -oy), tw, th)))
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# ── layers / frames ───────────────────────────────────────────────────────

def convert_to_paint(doc: Doc, l: Layer):
    if l.type != "shapes":
        return
    memo: dict = {}
    new = []
    for d in l.fr:
        if id(d) in memo:
            new.append(memo[id(d)])
            continue
        p = PaintData(doc.W, doc.H)
        compute_fills(d, doc.W, doc.H)
        if d.fc is not None:
            p.px[:] = d.fc
        px = p.px
        col_cache = {}
        for ln in d.lines:
            col = hex_rgb(ln["c"])
            pp = pts(ln, 64)
            for i in range(len(pp) - 1):
                a, b = pp[i], pp[i + 1]
                k = max(1, math.ceil(math.hypot(b[0] - a[0], b[1] - a[1]) * 2))
                for q in range(k + 1):
                    stamp(px, round(a[0] + (b[0] - a[0]) * q / k), round(a[1] + (b[1] - a[1]) * q / k), LW / 2, col, False)
        memo[id(d)] = p
        new.append(p)
    l.fr = new
    l.type = "paint"


def merge_down(doc: Doc, i: int, clear_fills: bool):
    u, lo = doc.layers[i], doc.layers[i - 1]
    if u.type != lo.type:
        convert_to_paint(doc, u if u.type == "shapes" else lo)
    kind = u.type
    memo: dict = {}
    fr = []
    for f in range(doc.F):
        a, b = lo.fr[f], u.fr[f]
        key = (id(a), id(b))
        if key not in memo:
            if kind == "shapes":
                n = ShapeData([dict(l) for l in a.lines + b.lines],
                              [] if clear_fills else [{**x, "set": None} for x in a.fills + b.fills])
            else:
                n = PaintData(doc.W, doc.H)
                n.px[:] = a.px
                over(n.px, b.px)
            memo[key] = n
        fr.append(memo[key])
    lo.fr, lo.type = fr, kind
    del doc.layers[i]


def fill_conflicts(doc: Doc, i: int) -> int:
    u, lo = doc.layers[i], doc.layers[i - 1]
    if u.type != "shapes" or lo.type != "shapes":
        return 0
    return sum(1 for f in range(doc.F) if lo.fr[f].fills and u.fr[f].fills)


# ── Castle Drawing2.drawData in and out ───────────────────────────────────

def _decode_png(b64: str) -> Optional[np.ndarray]:
    try:
        return np.asarray(Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGBA"))
    except Exception:
        return None


def _extract_fills(d: ShapeData, cvs: np.ndarray, W: int, H: int):
    """Castle fillPng -> buckets: one fill per same-colour connected region, seeded from its interior (never the
    sliver under the line), so they react to edits like buckets made here."""
    alpha = cvs[..., 3] > 0
    key = (cvs[..., 0].astype(np.int64) << 16) | (cvs[..., 1].astype(np.int64) << 8) | cvs[..., 2]
    seen = np.zeros((H, W), bool)
    for k in np.unique(key[alpha]):
        same = alpha & (key == k)
        ys, xs = np.nonzero(same & ~seen)
        for y0, x0 in zip(ys.tolist(), xs.tolist()):
            if seen[y0, x0]:
                continue
            idx, _ = flood(same, y0, x0, seen)
            m = np.zeros(H * W, bool)
            m[idx] = True
            m = m.reshape(H, W)
            depth, cur, last = 0, m, m
            while cur.any() and depth < 8:                      # erosion depth: seeds = pixels >= 6 deep
                e = cur.copy()
                e[1:] &= cur[:-1]; e[:-1] &= cur[1:]; e[:, 1:] &= cur[:, :-1]; e[:, :-1] &= cur[:, 1:]
                e[0] = e[-1] = False; e[:, 0] = e[:, -1] = False
                depth += 1
                last, cur = cur, e
                if depth == 5:
                    break
            seeds = np.flatnonzero((cur if cur.any() else last).reshape(-1))
            d.fills.append({"x": int(seeds[0] % W), "y": int(seeds[0] // W), "c": "#%06x" % int(k),
                            "px": seeds.astype(np.int64), "ok": 1})
    d.touch()


def load_castle(dd: dict) -> Doc:
    ppu = float(dd.get("fillPixelsPerUnit") or 25.6)
    half = max(5.0, float(dd.get("scale") or 10) / 2)
    ext = lambda *v: max([half] + [abs(x) for x in v])
    for b in dd.get("framesBounds") or []:
        half = ext(b.get("minX", 0), b.get("maxX", 0), b.get("minY", 0), b.get("maxY", 0))
    for L in dd.get("layers") or []:
        for f in L.get("frames") or []:
            b = f.get("fillImageBounds")
            if f.get("fillPng") and b:
                half = ext(b["minX"] / ppu, b["maxX"] / ppu, b["minY"] / ppu, b["maxY"] / ppu)
    W = min(2048, math.ceil(half * 2 * ppu) + 32)
    W += W & 1                                      # even: the canvas centre is a whole pixel, so export is exact
    H = W
    doc = Doc(W, H, ppu)
    doc.base = {k: v for k, v in dd.items() if k not in ("layers", "numTotalLayers", "framesBounds")}
    X, Y = (lambda v: W / 2 + v * ppu), (lambda v: H / 2 + v * ppu)
    F = max([1] + [len(l.get("frames") or []) for l in dd.get("layers") or []])
    doc.F = F
    for L in dd.get("layers") or []:
        kind = "paint" if L.get("isBitmap") else "shapes"
        fr: list = []
        for i, f in enumerate(L.get("frames") or []):
            if i and f.get("isLinked"):
                fr.append(fr[i - 1])
                continue
            d = new_data(kind, W, H)
            if kind == "shapes":
                for p in f.get("pathDataList") or []:
                    q = p["p"]
                    bp = p.get("bp")
                    d.lines.append({"a": (X(q[0]), Y(q[1])), "b": (X(q[2]), Y(q[3])),
                                    "m": (X(bp["x"]), Y(bp["y"])) if bp else None,
                                    "q": p.get("s") if (not bp and p.get("s") in (2, 3)) else 0,
                                    "c": c2h(p.get("c"))})
            png, b = f.get("fillPng"), f.get("fillImageBounds")
            if png and b:
                im = _decode_png(png)
                if im is not None:
                    canvas = np.zeros((H, W, 4), np.uint8)
                    ox, oy = round(W / 2 + b["minX"]), round(H / 2 + b["minY"])
                    h, w = im.shape[:2]
                    x0, y0, x1, y1 = max(0, ox), max(0, oy), min(W, ox + w), min(H, oy + h)
                    if x1 > x0 and y1 > y0:
                        canvas[y0:y1, x0:x1] = im[y0 - oy:y1 - oy, x0 - ox:x1 - ox]
                    if kind == "shapes":
                        _extract_fills(d, canvas, W, H)
                    else:
                        d.px = canvas
            fr.append(d)
        while len(fr) < F:
            fr.append(new_data(kind, W, H))
        doc.layers.append(Layer(L.get("title") or "Layer", kind, fr, L.get("isVisible") is not False, L.get("id")))
    if not doc.layers:
        doc.add_layer("shapes")
    return doc


def _crop(arr: np.ndarray, W: int, H: int):
    a = arr[..., 3]
    if not a.any():
        return None
    yy, xx = np.nonzero(a)
    x0, x1, y0, y1 = int(xx.min()), int(xx.max()), int(yy.min()), int(yy.max())
    sub = arr[y0:y1 + 1, x0:x1 + 1]
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(sub), "RGBA").save(buf, format="PNG", optimize=True)
    mx, my = round(x0 - W / 2), round(y0 - H / 2)
    return base64.b64encode(buf.getvalue()).decode(), {"minX": mx, "maxX": mx + (x1 - x0 + 1), "minY": my, "maxY": my + (y1 - y0 + 1)}


def to_draw(doc: Doc) -> dict:
    """Rebuild Castle's drawData from the editor layers. Units = (canvas px - W/2) / fillPixelsPerUnit."""
    ppu, W, H = doc.ppu, doc.W, doc.H
    r5 = lambda v: round(v * 1e5) / 1e5
    X, Y = (lambda v: r5((v - W / 2) / ppu)), (lambda v: r5((v - H / 2) / ppu))
    col = lambda h: [r5(c / 255) for c in hex_rgb(h)] + [1]
    FB: list = [None] * doc.F
    memo: dict = {}

    def ext(f, a, b, c, e):
        o = FB[f] or {"minX": 1e9, "maxX": -1e9, "minY": 1e9, "maxY": -1e9}
        FB[f] = {"minX": min(o["minX"], a), "maxX": max(o["maxX"], b), "minY": min(o["minY"], c), "maxY": max(o["maxY"], e)}

    def fd(l, d):
        if id(d) in memo:
            return memo[id(d)]
        r = {"paths": [], "png": None, "b": None, "box": None}
        c = None
        if isinstance(d, ShapeData):
            x0 = y0 = 1e9
            x1 = y1 = -1e9
            for n in d.lines:
                p = {"p": [X(n["a"][0]), Y(n["a"][1]), X(n["b"][0]), Y(n["b"][1])], "s": 1, "f": False, "c": col(n["c"])}
                if n.get("q"):
                    p["s"] = n["q"]
                elif n.get("m"):
                    m = norm_m(n["a"], n["m"], n["b"])
                    if m:
                        p["bp"] = {"x": X(m[0]), "y": Y(m[1])}
                r["paths"].append(p)
                for u, v in pts(n):
                    x0, x1, y0, y1 = min(x0, X(u)), max(x1, X(u)), min(y0, Y(v)), max(y1, Y(v))
            if r["paths"]:
                r["box"] = [x0, x1, y0, y1]
            compute_fills(d, W, H)
            if d.fc is not None and any(s.get("ok") for s in d.fills):
                c = _crop(d.fc, W, H)
        else:
            c = _crop(d.px, W, H)
        if c:
            r["png"], r["b"] = c
            b = c[1]
            bx = [b["minX"] / ppu, b["maxX"] / ppu, b["minY"] / ppu, b["maxY"] / ppu]
            r["box"] = bx if not r["box"] else [min(r["box"][0], bx[0]), max(r["box"][1], bx[1]),
                                                min(r["box"][2], bx[2]), max(r["box"][3], bx[3])]
        memo[id(d)] = r
        return r

    out = dict(doc.base)
    layers = []
    for li, l in enumerate(doc.layers):
        frames = []
        for fi, d in enumerate(l.fr):
            r = fd(l, d)
            lk = doc.linked(l, fi)
            f = {"isLinked": lk, "pathDataList": [] if lk else r["paths"],
                 "fillImageBounds": {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0} if lk or not r["b"] else r["b"],
                 "avatarX": 0, "avatarY": 0, "avatarRadius": 5}
            if not lk and r["png"]:
                f["fillPng"] = r["png"]
            if r["box"]:
                ext(fi, *r["box"])
            frames.append(f)
        layers.append({"title": l.name, "id": l.id or f"layer{li + 1}", "isVisible": l.vis, "isBitmap": l.type == "paint",
                       "isAvatar": False, "frames": frames})
    out["layers"] = layers
    out["numTotalLayers"] = len(layers)
    out["framesBounds"] = [({k: r5(v) for k, v in o.items()} if o else {"minX": -5, "maxX": 5, "minY": -5, "maxY": 5}) for o in FB]
    return out
