"""Layer editing for a blueprint's Drawing2 (`drawData.layers`).

Castle stores a drawing as a list of layers, bottom first. Every layer has one
entry per animation frame in `frames`. A layer is either vector (`pathDataList`)
or bitmap (`fillPng` + `fillImageBounds`, in pixels; units = pixels /
`drawData.fillPixelsPerUnit`). Vector coordinates are already in Castle units.

All functions here are pure (they work on plain dicts) and raise LayerError with
a message fit for the user.
"""
from __future__ import annotations

import base64
import copy
import io
import json
from typing import Optional

NEW = "new"                      # target for "add a new layer"
DEFAULT_BOUNDS = {"minX": -5, "maxX": 5, "minY": -5, "maxY": 5}


class LayerError(Exception):
    """Something the user should read."""


# ── reading ──────────────────────────────────────────────────────────────

def _draw(d2: Optional[dict]) -> dict:
    return (d2 or {}).get("drawData") or {}


def layers_of(d2: Optional[dict]) -> list:
    return _draw(d2).get("layers") or []


def summary(d2: Optional[dict]) -> list[dict]:
    """[{id, title, bitmap}] in storage order (bottom layer first)."""
    return [{"id": l.get("id"), "title": l.get("title") or "", "bitmap": bool(l.get("isBitmap"))}
            for l in layers_of(d2)]


def frame_count(d2: Optional[dict]) -> int:
    n = len(_draw(d2).get("framesBounds") or [])
    for l in layers_of(d2):
        n = max(n, len(l.get("frames") or []))
    return n


def index_of(d2: Optional[dict], layer_id: str) -> int:
    for i, l in enumerate(layers_of(d2)):
        if l.get("id") == layer_id:
            return i
    raise LayerError("That layer no longer exists.")


def _blank_frame() -> dict:
    return {"isLinked": False, "pathDataList": [],
            "fillImageBounds": {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0},
            "avatarX": 0, "avatarY": 0, "avatarRadius": 5}


def _rehash(d2: dict) -> None:
    d2["hash"] = str(abs(hash(json.dumps(layers_of(d2), sort_keys=True))))[:19]


# ── bitmap density ───────────────────────────────────────────────────────

def _rescale_bitmap_frames(frames: list, factor: float) -> None:
    """Resize every bitmap frame by `factor`, in place (nearest neighbour, so pixels stay crisp)."""
    from PIL import Image
    for fr in frames:
        png, b = fr.get("fillPng"), fr.get("fillImageBounds")
        if not png or not b:
            continue
        w = max(1, round((b["maxX"] - b["minX"]) * factor))
        h = max(1, round((b["maxY"] - b["minY"]) * factor))
        cx, cy = (b["minX"] + b["maxX"]) / 2 * factor, (b["minY"] + b["maxY"]) / 2 * factor
        img = Image.open(io.BytesIO(base64.b64decode(png))).convert("RGBA").resize((w, h), Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        fr["fillPng"] = base64.b64encode(buf.getvalue()).decode()
        fr["fillImageBounds"] = {"minX": round(cx - w / 2, 5), "maxX": round(cx + w / 2, 5),
                                 "minY": round(cy - h / 2, 5), "maxY": round(cy + h / 2, 5)}


def _unit_bounds(frame: dict, ppu: float) -> Optional[dict]:
    """Where a frame's content sits, in Castle units (None when it is empty)."""
    xs: list[float] = []
    ys: list[float] = []
    for seg in frame.get("pathDataList") or []:
        p = seg.get("p") or []
        if len(p) >= 4:
            xs += [p[0], p[2]]
            ys += [p[1], p[3]]
    b = frame.get("fillImageBounds")
    if frame.get("fillPng") and b and ppu:
        xs += [b["minX"] / ppu, b["maxX"] / ppu]
        ys += [b["minY"] / ppu, b["maxY"] / ppu]
    if not xs:
        return None
    return {"minX": min(xs), "maxX": max(xs), "minY": min(ys), "maxY": max(ys)}


def _union(a: dict, b: dict) -> dict:
    return {"minX": min(a["minX"], b["minX"]), "maxX": max(a["maxX"], b["maxX"]),
            "minY": min(a["minY"], b["minY"]), "maxY": max(a["maxY"], b["maxY"])}


# ── editing ──────────────────────────────────────────────────────────────

def apply_drawing(existing: Optional[dict], new: dict, target: Optional[str]) -> dict:
    """Put the freshly imported drawing `new` (one layer) into `existing`.

    target None  → replace the whole Drawing2 (what a plain import does)
    target NEW   → add `new`'s layer on top as a new layer
    target <id>  → overwrite that layer; a bitmap file makes it a bitmap layer and a
                   vector file makes it a vector layer, whatever it was before
    Other layers are never touched, except that they get empty frames when the import
    has more frames than the drawing had.
    """
    if target is None or not layers_of(existing) or not layers_of(new):
        return copy.deepcopy(new)

    ex_layers = layers_of(existing)
    idx = -1 if target == NEW else index_of(existing, target)
    if idx >= 0 and len(ex_layers) == 1:
        # the only layer: the import simply becomes the drawing (size, density, physics and all)
        out = copy.deepcopy(new)
        keep, fresh = ex_layers[0], layers_of(out)[0]
        for key in ("title", "id", "isVisible"):
            if key in keep:
                fresh[key] = keep[key]
        _rehash(out)
        return out

    out = copy.deepcopy(existing)
    d = out["drawData"]
    src = copy.deepcopy(layers_of(new)[0])
    ppu = float(d.get("fillPixelsPerUnit") or 0) or float(_draw(new).get("fillPixelsPerUnit") or 1)
    ppu_new = float(_draw(new).get("fillPixelsPerUnit") or ppu)
    if src.get("isBitmap") and abs(ppu - ppu_new) > 1e-6:
        _rescale_bitmap_frames(src["frames"], ppu / ppu_new)      # keep its size in Castle units

    old_n, new_n = frame_count(existing), len(src["frames"])
    total = max(old_n, new_n)
    frames = src["frames"] + [_blank_frame() for _ in range(total - new_n)]

    layers = d["layers"]
    if idx < 0:
        n = int(d.get("numTotalLayers") or len(layers)) + 1
        taken = {l.get("id") for l in layers}
        while f"layer{n}" in taken:
            n += 1
        layers.append({"title": f"Layer {n}", "id": f"layer{n}", "isVisible": True,
                       "isBitmap": bool(src.get("isBitmap")), "isAvatar": False, "frames": frames})
        d["numTotalLayers"] = n
        target_pos = len(layers) - 1
    else:
        layers[idx] = {**layers[idx], "isBitmap": bool(src.get("isBitmap")), "isAvatar": False, "frames": frames}
        target_pos = idx

    for i, l in enumerate(layers):                   # longer import: every other layer gets empty frames
        if i != target_pos:
            l["frames"] = (l.get("frames") or []) + [_blank_frame() for _ in range(total - len(l.get("frames") or []))]

    bounds = list(d.get("framesBounds") or [])
    bounds += [dict(DEFAULT_BOUNDS) for _ in range(total - len(bounds))]
    for i, fr in enumerate(frames):
        ub = _unit_bounds(fr, ppu)
        if ub:
            bounds[i] = _union(bounds[i], ub)
    d["framesBounds"] = bounds

    if new_n > old_n and old_n <= 1:                 # a still drawing just became an animation
        for key in ("framesPerSecond", "playMode", "loop"):
            if key in new:
                out[key] = new[key]
    _rehash(out)
    return out


def rename_layer(d2: dict, layer_id: str, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise LayerError("A layer needs a name.")
    layers_of(d2)[index_of(d2, layer_id)]["title"] = name
    _rehash(d2)


def delete_layer(d2: dict, layer_id: str) -> None:
    layers = layers_of(d2)
    i = index_of(d2, layer_id)
    if len(layers) <= 1:
        raise LayerError("A drawing needs at least one layer.")
    del layers[i]
    _rehash(d2)


def move_layer(d2: dict, old_index: int, new_index: int) -> None:
    """Storage-order indices (0 = bottom layer)."""
    layers = layers_of(d2)
    if not (0 <= old_index < len(layers)) or not (0 <= new_index < len(layers)):
        raise LayerError("That layer can't go there.")
    layers.insert(new_index, layers.pop(old_index))
    _rehash(d2)
