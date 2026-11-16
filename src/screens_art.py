"""Art editor screen: draw Castle drawings (vector shapes + paint layers, frames) right on the phone.

One finger draws, two fingers pan and zoom. The top bar has Back (exits the editor; the drawing is written back
into the blueprint then), Undo and Redo. Everything is edited in memory (art_engine.Doc) and written to the blueprint's
Drawing2.drawData once, when you leave.
"""
from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import flet as ft

import art_engine as art
import blueprint_ops as ops
import layers as lyr
import theme
from app import Screen
from palettes import PALETTE_NAMES, PALETTES
from theme import BG, DANGER, MAJOR, MINOR1, SEP, castle_icon, text
from widgets import icon_button, open_color_picker, pill_button

TOOLS = {"shapes": ["pencil", "bucket", "grab", "eraser"], "paint": ["brush", "spray", "bucket", "grab", "eraser"]}
TOOL_ICON = {"brush": "brush", "spray": "spray"}
ER, PER = [5, 12, 24], [3, 8, 16]            # eraser radii: shapes / paint
UNDO_MAX = 40
CELL = 38
FT_ICONS = {"brush": ft.Icons.BRUSH, "spray": ft.Icons.BLUR_ON, "cut": ft.Icons.CONTENT_CUT,
            "vector_radius": ft.Icons.ROUNDED_CORNER, "cursor_move": ft.Icons.OPEN_WITH,
            "rotate-right": ft.Icons.ROTATE_RIGHT, "flip_h": ft.Icons.FLIP, "flip_v": ft.Icons.FLIP,
            "shapes": ft.Icons.CATEGORY, "dots": ft.Icons.MORE_HORIZ, "picture": ft.Icons.IMAGE,
            "eye": ft.Icons.VISIBILITY, "eyeoff": ft.Icons.VISIBILITY_OFF, "link": ft.Icons.LINK,
            "unlink": ft.Icons.LINK_OFF, "modsq": ft.Icons.CROP_SQUARE, "copy": ft.Icons.CONTENT_COPY,
            "paste": ft.Icons.CONTENT_PASTE, "merge": ft.Icons.CALL_MERGE, "convert": ft.Icons.BRUSH}


def ico(name: str, size: float = 22, color: str = MAJOR, rot: float = 0) -> ft.Control:
    """Castle glyph when the icon font has it, else a stock icon / a drawn dot."""
    if name in ("e1", "e2", "e3"):
        d = {"e1": 5, "e2": 10, "e3": 16}[name] * size / 22
        c: ft.Control = ft.Container(width=d, height=d, border_radius=d / 2, bgcolor=color)
    elif name in theme.CASTLE:
        c = castle_icon(name, size, color)
    else:
        c = ft.Icon(FT_ICONS[name], size=size, color=color)
        if name == "flip_v":
            rot = rot or 90
    return ft.Container(c, rotate=ft.Rotate(angle=math.radians(rot)), alignment=ft.Alignment.CENTER) if rot else c


def tbtn(icon: str, on_click, on: bool = False, size: int = 44, rot: float = 0, disabled: bool = False,
         tooltip: Optional[str] = None) -> ft.Control:
    """Dock button: active = white with black glyph (Castle style)."""
    return ft.Container(ico(icon, 22, BG if on else MAJOR, rot), width=size, height=size, border_radius=12,
                        alignment=ft.Alignment.CENTER, bgcolor=MAJOR if on else BG,
                        border=ft.Border.all(1, MAJOR if on else SEP), ink=not disabled, tooltip=tooltip,
                        on_click=None if disabled else on_click, opacity=0.3 if disabled else 1.0)


def dock(children: list, horizontal: bool = False) -> ft.Control:
    box = (ft.Row if horizontal else ft.Column)(children, spacing=6, tight=True)
    return ft.Container(box, padding=6, bgcolor=BG, border_radius=16, border=ft.Border.all(1, SEP))


class ArtEditor:
    def __init__(self, app, ctx, bp_path: Path, doc: art.Doc, view=None):
        self.app, self.ctx, self.bp_path, self.doc, self.bp_view = app, ctx, Path(bp_path), doc, view
        self.sel = len(doc.layers) - 1
        self.cf = 0
        self.tool = TOOLS[doc.layers[self.sel].type][0]
        self.sub = {"p": "squiggle", "g": "modify", "e": "cut", "b": "fill"}
        self.size = 10
        self.color = "#000000"
        self.pip = False
        self.dd = False
        self.tab = "pal"
        self.pal = 0
        self.open = True
        self.clip = None
        self.undo: list = []
        self.redo: list = []
        self.changed = False
        self.strk: Optional[dict] = None             # the stroke being drawn
        self.pend = None                             # (snapshot, time) for "a second finger cancels the stroke"
        self.tmp: Optional[list] = None
        self.pinch: Optional[dict] = None
        self.vw, self.vh = (getattr(app.page, "width", None) or 360), 420
        self.view = {"z": 1.0, "x": 0.0, "y": 0.0}
        self.thumbs: dict = {}
        self.alive = True
        self.img = ft.Image(src=art.to_png(art.render_doc(doc, 0, self.sel)), fit=ft.BoxFit.FILL, gapless_playback=True,
                            filter_quality=ft.FilterQuality.NONE, width=doc.W, height=doc.H, left=0, top=0)
        self.canvas = ft.Stack([self.img], expand=True, clip_behavior=ft.ClipBehavior.HARD_EDGE)
        self.detector = ft.GestureDetector(
            content=ft.Container(self.canvas, bgcolor="#222222", expand=True),
            on_tap_down=self._tap_down, on_tap_up=self._tap_up,
            on_scale_start=self._scale_start, on_scale_update=self._scale_update, on_scale_end=self._scale_end,
            on_scroll=self._wheel, expand=True)
        self.viewport = ft.Container(self.detector, expand=True, on_size_change=self._resized)
        self.left_dock = ft.Container(left=6, top=8)
        self.right_top = ft.Container(right=6, top=8)
        self.sub_dock = ft.Container(right=6, top=64)
        self.size_row = ft.Container(left=70, right=70, bottom=6)
        self.palette_box = ft.Container(right=6, top=60)
        self.overlay = ft.Stack([self.viewport, self.left_dock, self.right_top, self.sub_dock, self.palette_box,
                                 self.size_row], expand=True)
        self.drawer = ft.Container()
        self.root = ft.Column([self.overlay, self.drawer], spacing=0, expand=True)
        self.go_home()
        self.rebuild()

    # ── helpers ───────────────────────────────────────────────────────────
    @property
    def layer(self) -> Optional[art.Layer]:
        return self.doc.layers[self.sel] if 0 <= self.sel < len(self.doc.layers) else None

    def cur(self):
        l = self.layer
        return (l, l.fr[self.cf]) if l else None

    def tools(self) -> list:
        l = self.layer
        return TOOLS[l.type] if l else []

    def fix_tool(self):
        t = self.tools()
        if t and self.tool not in t:
            self.tool = t[0]

    def to_art(self, x: float, y: float):
        return ((x - self.view["x"]) / self.view["z"], (y - self.view["y"]) / self.view["z"])

    def go_home(self):
        W, H = self.doc.W, self.doc.H
        z = max(0.3, min((self.vw - 24) / W, (self.vh - 24) / H, 2.2))
        self.view = {"z": z, "x": (self.vw - W * z) / 2, "y": max(8.0, (self.vh - H * z) / 2)}

    def _resized(self, e):
        w, h = getattr(e, "width", None), getattr(e, "height", None)
        if not w or not h or (abs(w - self.vw) < 1 and abs(h - self.vh) < 1):
            return
        first = not getattr(self, "_sized", False)
        self._sized, self.vw, self.vh = True, w, h
        if first:
            self.go_home()
        self.place()
        self.app.page.update()

    def place(self):
        v = self.view
        self.img.left, self.img.top = v["x"], v["y"]
        self.img.width, self.img.height = self.doc.W * v["z"], self.doc.H * v["z"]

    # ── drawing the canvas ────────────────────────────────────────────────
    def handles(self):
        l = self.layer
        if l and l.type == "shapes" and self.tool == "grab" and self.sub["g"] in ("modify", "arc"):
            return (self.sub["g"],)
        return None

    def render(self, update: bool = True):
        arr = art.render_doc(self.doc, self.cf, self.sel, self.tmp, self.handles(), self.view["z"])
        self.img.src = art.to_png(arr)
        self.place()
        if update:
            self.app.page.update()

    # ── undo / redo ───────────────────────────────────────────────────────
    def push_undo(self, snap=None):
        self.undo.append(snap if snap is not None else self.doc.snapshot())
        del self.undo[:-UNDO_MAX]
        self.redo.clear()
        self.changed = True
        self.thumbs.clear()
        self.app.refresh_bar()

    def do_undo(self, e=None):
        if not self.undo:
            return
        self.redo.append(self.doc.snapshot())
        self.doc.restore(self.undo.pop())
        self.after_history()

    def do_redo(self, e=None):
        if not self.redo:
            return
        self.undo.append(self.doc.snapshot())
        self.doc.restore(self.redo.pop())
        self.after_history()

    def after_history(self):
        self.changed = True
        self.thumbs.clear()
        self.sel = max(0, min(self.sel, len(self.doc.layers) - 1))
        self.cf = min(self.cf, self.doc.F - 1)
        self.fix_tool()
        self.rebuild()
        self.app.refresh_bar()

    # ── input ─────────────────────────────────────────────────────────────
    def _tap_down(self, e):
        if self.strk or self.pinch:
            return
        p = self.to_art(e.local_position.x, e.local_position.y)
        self.down(p)

    def _tap_up(self, e):
        if self.strk:
            self.finish()

    def _scale_start(self, e):
        if e.pointer_count >= 2:
            self.cancel_pending()
            f = e.local_focal_point
            self.pinch = {"f": (f.x, f.y), "v": dict(self.view)}

    def _scale_update(self, e):
        f = e.local_focal_point
        if e.pointer_count >= 2:
            if not self.pinch:
                self.cancel_pending()
                self.pinch = {"f": (f.x, f.y), "v": dict(self.view), "s": 1.0}
            pv = self.pinch["v"]
            z = max(0.3, min(12.0, pv["z"] * e.scale))
            k = z / pv["z"]
            self.view = {"z": z, "x": f.x - (self.pinch["f"][0] - pv["x"]) * k, "y": f.y - (self.pinch["f"][1] - pv["y"]) * k}
            self.place()
            self.app.page.update()
            return
        if self.pinch:
            return
        if self.strk:
            self.move(self.to_art(f.x, f.y))

    def _scale_end(self, e):
        if self.pinch and e.pointer_count == 0:
            if self.handles():
                self.render()
            self.pinch = None
        elif self.strk and e.pointer_count == 0:
            self.finish()

    def _wheel(self, e):
        dy = getattr(e.scroll_delta, "y", 0) if hasattr(e, "scroll_delta") else 0
        pos = getattr(e, "local_position", None)
        if not dy or pos is None:
            return
        v = self.view
        z = max(0.3, min(12.0, v["z"] * math.exp(-dy * 0.0015)))
        k = z / v["z"]
        self.view = {"z": z, "x": pos.x - (pos.x - v["x"]) * k, "y": pos.y - (pos.y - v["y"]) * k}
        self.place()
        self.app.page.update()

    def cancel_pending(self):
        """A second finger within a third of a second means the first was the start of a pan, not a stroke."""
        if self.strk and self.pend and time.monotonic() - self.pend[1] < 0.35:
            self.doc.restore(self.pend[0])
            self.undo = self.undo[:self.pend[2]]
        self.strk, self.pend, self.tmp = None, None, None
        self.render()

    # ── tools ─────────────────────────────────────────────────────────────
    def down(self, p):
        doc = self.doc
        if self.pip:
            for i in range(len(doc.layers) - 1, -1, -1):
                l = doc.layers[i]
                if not l.vis:
                    continue
                c = art.color_at(l, l.fr[self.cf], p, doc.W, doc.H)
                if c:
                    self.color = c
                    break
            self.pip = False
            self.rebuild()
            return
        c = self.cur()
        if not c or not c[0].vis:
            return
        l, d = c
        t, shapes = self.tool, l.type == "shapes"
        snap = doc.snapshot()
        self.pend = (snap, time.monotonic(), len(self.undo))
        k: Optional[dict] = None
        if t == "bucket":
            if self.sub["b"] == "swap":
                art.swap_all(l, d, p, self.color, doc.W, doc.H)
            elif shapes:
                art.shape_bucket(d, p, self.color, doc.W, doc.H)
            else:
                art.paint_fill(d, p, self.color)
            self.push_undo(snap)
            self.thumbs.clear()
            self.render()
            self.rebuild_drawer()
            self.pend = None
            return
        if shapes:
            if t == "pencil":
                k = {"k": "draw", "p0": p, "raw": [p]}
            elif t == "grab":
                g = self.sub["g"]
                if g == "move":
                    k = {"k": "mv", "l": p}
                else:
                    best = art.pick_handle(d, g, p, 20 / self.view["z"])
                    if best is not None:
                        k = {"k": "mod", "c": tuple(best)} if g == "modify" else {"k": "arc", "l": best}
            elif t == "eraser":
                k = {"k": "er"}
                self.erase(d, p)
        elif t == "grab":
            d.own()
            k = {"k": "pmv", "l": p, "base": d.px.copy()}
        elif t == "eraser" and self.sub["e"] == "cut":
            art.paint_fill(d, p, None)
            self.push_undo(snap)
            self.render()
            self.pend = None
            return
        else:
            d.own()
            k = {"k": "pt", "l": p}
            self.paint_at(d, p, p)
        if k:
            self.strk = k
            self.strk["snap"] = snap
            self.render()
        else:
            self.pend = None

    def erase(self, d, p):
        cut = self.sub["e"] == "cut"
        r = 6 if cut else ER[int(self.sub["e"]) - 1]
        art.erase_shapes(d, p, r, cut)

    def paint_at(self, d, a, b):
        er = self.tool == "eraser"
        if self.tool == "spray":
            art.spray(d, b, self.color, self.size)
        else:
            r = PER[int(self.sub["e"]) - 1] if er and self.sub["e"] != "cut" else self.size / 2
            art.paint_line(d, a, b, self.color, er, r)

    def move(self, p):
        c = self.cur()
        if not c or not self.strk:
            return
        l, d = c
        k = self.strk
        kind = k["k"]
        if kind == "draw":
            if self.sub["p"] == "squiggle":
                last = k["raw"][-1]
                if math.hypot(p[0] - last[0], p[1] - last[1]) > 1.5:
                    k["raw"].append(p)
            else:
                k["raw"] = [k["p0"], p]
            self.tmp = art.make_shape(self.sub["p"], k["p0"], k["raw"], self.color)
        elif kind == "mod":
            old = k["c"]
            art.move_handle(d, old, p)
            k["c"] = p
        elif kind == "arc":
            ln = k["l"]
            ln["q"] = 0
            ln["m"] = art.norm_m(ln["a"], p, ln["b"])
            d.touch()
        elif kind == "mv":
            dx, dy = round(p[0] - k["l"][0]), round(p[1] - k["l"][1])
            if not dx and not dy:
                return
            art.move_shapes(d, dx, dy)
            art.shift_fill_pixels(d, dx, dy, self.doc.W, self.doc.H)
            k["l"] = (k["l"][0] + dx, k["l"][1] + dy)
        elif kind == "er":
            self.erase(d, p)
        elif kind == "pt":
            self.paint_at(d, k["l"], p)
            k["l"] = p
        elif kind == "pmv":
            px = d.px
            px[:] = 0
            dx, dy = round(p[0] - k["l"][0]), round(p[1] - k["l"][1])
            base = k["base"]
            H, W = base.shape[:2]
            sx0, sy0 = max(0, -dx), max(0, -dy)
            sx1, sy1 = min(W, W - dx), min(H, H - dy)
            if sx1 > sx0 and sy1 > sy0:
                px[sy0 + dy:sy1 + dy, sx0 + dx:sx1 + dx] = base[sy0:sy1, sx0:sx1]
            d.cache = None
        self.render()

    def finish(self):
        k, self.strk = self.strk, None
        c = self.cur()
        if k and c:
            l, d = c
            if k["k"] == "draw" and self.tmp:
                for ln in self.tmp:
                    if math.hypot(ln["b"][0] - ln["a"][0], ln["b"][1] - ln["a"][1]) > .5:
                        d.lines.append(dict(ln))
                d.touch()
            self.push_undo(k["snap"])
        self.tmp, self.pend = None, None
        self.render()
        self.rebuild_drawer()

    # ── transforms / layer ops (one undo step each) ───────────────────────
    def commit_op(self, fn):
        snap = self.doc.snapshot()
        fn()
        self.push_undo(snap)
        self.after_op()

    def after_op(self):
        self.sel = max(0, min(self.sel, len(self.doc.layers) - 1))
        self.fix_tool()
        self.rebuild()

    def xf(self, kind: str):
        c = self.cur()
        if c:
            self.commit_op(lambda: art.transform(self.doc, c[1], kind))

    # ── UI pieces ─────────────────────────────────────────────────────────
    def rebuild(self):
        self.fix_tool()
        l = self.layer
        adds = self.tool in ("pencil", "bucket", "brush", "spray") and l is not None
        self.left_dock.content = dock([tbtn(t, lambda e, t=t: self.set_tool(t), self.tool == t) for t in self.tools()]) if self.tools() else None
        self.sub_dock.content = self.sub_dock_content()
        self.right_top.content = ft.Row([
            ft.Container(width=44, height=44, border_radius=12, bgcolor=self.color, border=ft.Border.all(2, MAJOR),
                         ink=True, on_click=lambda e: self.toggle_dd()),
            tbtn("pipette", lambda e: self.toggle_pip(), self.pip)], spacing=6, tight=True) if adds else None
        self.palette_box.content = self.palette_content() if (adds and self.dd) else None
        self.size_row.content = self.size_slider() if (l and l.type == "paint" and self.tool in ("brush", "spray")) else None
        self.rebuild_drawer(update=False)
        self.render(update=False)
        self.app.page.update()

    def set_tool(self, t: str):
        self.tool, self.pip = t, False
        self.rebuild()

    def toggle_dd(self):
        self.dd = not self.dd
        self.rebuild()

    def toggle_pip(self):
        self.pip = not self.pip
        self.rebuild()

    def sub_dock_content(self):
        l = self.layer
        if not l:
            return None
        T, ty = self.tool, l.type
        sub = self.sub

        def opt(icon, key, val, rot=0):
            return tbtn(icon, lambda e: self.set_sub(key, val), str(sub[key]) == str(val), 40, rot)
        tr = [tbtn("rotate-right", lambda e: self.xf("rot"), size=40), tbtn("flip_h", lambda e: self.xf("fh"), size=40),
              tbtn("flip_v", lambda e: self.xf("fv"), size=40)]
        er = [opt("cut", "e", "cut")] + [opt(f"e{n}", "e", n) for n in (1, 2, 3)]
        if T == "bucket":
            return dock([opt("bucket", "b", "fill"), opt("swap", "b", "swap")])
        if ty == "shapes":
            if T == "pencil":
                return dock([opt(k, "p", k) for k in ("squiggle", "line", "square", "circle", "triangle")])
            if T == "grab":
                return dock([opt("modsq", "g", "modify"), opt("vector_radius", "g", "arc"), opt("cursor_move", "g", "move")] + tr)
            if T == "eraser":
                return dock(er)
        if T == "grab":
            return dock(tr)
        if T == "eraser":
            return dock(er)
        return None

    def set_sub(self, key: str, val):
        self.sub[key] = val
        self.rebuild()

    def size_slider(self):
        lab = text(str(self.size), 13, MAJOR, bold=True)

        def change(e):
            self.size = int(e.control.value)
            lab.value = str(self.size)
            self.app.page.update()
        return ft.Container(ft.Row([lab, ft.Slider(min=1, max=60, divisions=59, value=self.size, on_change=change,
                                                   active_color=MAJOR, thumb_color=MAJOR, expand=True)],
                                   spacing=8), bgcolor=BG, border_radius=16, border=ft.Border.all(1, SEP),
                            padding=ft.Padding(left=12, right=4, top=0, bottom=0))

    def palette_content(self):
        def tab(k, n):
            return ft.Container(text(n, 13, BG if self.tab == k else MINOR1, bold=self.tab == k), padding=ft.Padding.symmetric(vertical=6, horizontal=12),
                                border_radius=10, bgcolor=MAJOR if self.tab == k else None, ink=True, on_click=lambda e: self.set_tab(k))
        body: ft.Control
        if self.tab == "pal":
            name = PALETTE_NAMES[self.pal]
            cols = PALETTES[name]
            grid = ft.Row([ft.Container(width=26, height=26, border_radius=6, bgcolor="#%02x%02x%02x" % c,
                                        border=ft.Border.all(2, MAJOR if "#%02x%02x%02x" % c == self.color else SEP),
                                        on_click=lambda e, c=c: self.pick("#%02x%02x%02x" % c)) for c in cols],
                          wrap=True, spacing=4, run_spacing=4, width=224)
            head = ft.Row([text(f"{name} ({len(cols)})", 12, MINOR1, expand=True),
                           icon_button(castle_icon("left_arrow", 16), lambda e: self.step_pal(-1), size=30),
                           icon_button(castle_icon("left_arrow", 16, MAJOR), lambda e: self.step_pal(1), size=30)], spacing=0,
                          width=224)
            body = ft.Column([head, ft.Container(grid, height=min(230, 34 * math.ceil(len(cols) / 7) + 4))], spacing=4, tight=True,
                             scroll=ft.ScrollMode.AUTO)
        else:
            body = ft.Container(pill_button("Pick a color...", lambda e: open_color_picker(self.app, self.color, self.pick), filled=True),
                                padding=ft.Padding.symmetric(vertical=14), width=224, alignment=ft.Alignment.CENTER)
        return ft.Container(ft.Column([ft.Row([tab("pal", "Palette"), tab("pick", "Color")], spacing=4), body], spacing=6, tight=True),
                            padding=8, bgcolor=BG, border_radius=16, border=ft.Border.all(1, SEP), width=244, height=min(self.vh - 70, 330))

    def set_tab(self, k):
        self.tab = k
        self.rebuild()

    def step_pal(self, d):
        self.pal = (self.pal + d) % len(PALETTE_NAMES)
        self.rebuild()

    def pick(self, c: str):
        self.color = c
        self.rebuild()

    # ── layers × frames drawer ────────────────────────────────────────────
    def thumb(self, i: int, f: int) -> ft.Control:
        l = self.doc.layers[i]
        d = l.fr[f]
        sel = i == self.sel and f == self.cf
        key = (id(d), sel)
        if key not in self.thumbs:
            self.thumbs[key] = art.thumb(self.doc, l, d, 76, sel)
        return ft.Image(src=self.thumbs[key], width=CELL, height=CELL, fit=ft.BoxFit.FILL, filter_quality=ft.FilterQuality.NONE)

    def rebuild_drawer(self, update: bool = True):
        doc = self.doc
        head = ft.Row([tbtn("add", lambda e: self.add_menu(), size=36),
                       ft.Container(ico("left_arrow", 18, MAJOR, -90 if self.open else 90), width=36, height=36, ink=True,
                                    border_radius=18, alignment=ft.Alignment.CENTER, on_click=lambda e: self.toggle_open())],
                      spacing=6)
        if not self.open:
            self.drawer.content = ft.Container(head, padding=ft.Padding(left=8, right=8, top=4, bottom=4),
                                               border=ft.Border(top=ft.BorderSide(1, SEP)))
        else:
            rows = []
            for i in range(len(doc.layers) - 1, -1, -1):
                l = doc.layers[i]
                cells = []
                for f in range(doc.F):
                    sel = i == self.sel and f == self.cf
                    linked = doc.linked(l, f)
                    inner = (ft.Container(ico("link", 16, MINOR1), width=CELL, height=CELL, alignment=ft.Alignment.CENTER)
                             if linked else self.thumb(i, f))
                    cells.append(ft.Container(inner, width=CELL + 2, height=CELL + 2, padding=1, border_radius=6,
                                              border=ft.Border.all(2 if sel else 1, MAJOR if sel else SEP), ink=True,
                                              on_click=lambda e, i=i, f=f: self.cell_tap(i, f)))
                eye = ft.Container(ico("eye" if l.vis else "eyeoff", 18, MAJOR if i == self.sel else MINOR1), width=34, height=34,
                                   alignment=ft.Alignment.CENTER, ink=True, border_radius=17,
                                   on_click=lambda e, i=i: self.toggle_vis(i))
                name = ft.Container(text(l.name, 13, BG if i == self.sel else MAJOR, max_lines=1, no_wrap=True,
                                         overflow=ft.TextOverflow.ELLIPSIS), width=84, padding=ft.Padding.symmetric(vertical=8, horizontal=8),
                                    border_radius=8, bgcolor=MAJOR if i == self.sel else None, ink=True,
                                    on_click=lambda e, i=i: self.layer_tap(i))
                rows.append(ft.Row([eye, name] + cells, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER))
            nums = ft.Row([ft.Container(width=122)] + [
                ft.Container(text(str(f + 1), 12, BG if f == self.cf else MINOR1, bold=f == self.cf), width=CELL + 2, height=26,
                             alignment=ft.Alignment.CENTER, border_radius=8, bgcolor=MAJOR if f == self.cf else None, ink=True,
                             on_click=lambda e, f=f: self.frame_tap(f)) for f in range(doc.F)] +
                [tbtn("add", lambda e: self.add_frame(), size=30)], spacing=4)
            grid = ft.Row([ft.Column(rows + [nums], spacing=4, tight=True)], scroll=ft.ScrollMode.AUTO)
            self.drawer.content = ft.Container(ft.Column([head, ft.Container(ft.Column([grid], scroll=ft.ScrollMode.AUTO),
                                                                              height=min(self.vh * 0.5, 44 * (len(rows) + 1) + 10))],
                                                         spacing=2, tight=True),
                                               padding=ft.Padding(left=8, right=8, top=4, bottom=8), bgcolor=BG,
                                               border=ft.Border(top=ft.BorderSide(1, SEP)))
        if update:
            self.app.page.update()

    def toggle_open(self):
        self.open = not self.open
        self.rebuild_drawer()

    def toggle_vis(self, i):
        self.commit_op(lambda: setattr(self.doc.layers[i], "vis", not self.doc.layers[i].vis))

    def layer_tap(self, i):
        if i == self.sel:
            self.layer_menu()
        else:
            self.sel = i
            self.rebuild()

    def cell_tap(self, i, f):
        if i == self.sel and f == self.cf:
            self.cell_menu()
        else:
            self.sel, self.cf = i, f
            self.rebuild()

    def frame_tap(self, f):
        if self.cf == f:
            self.frame_menu()
        else:
            self.cf = f
            self.rebuild()

    # ── menus (dialogs) ───────────────────────────────────────────────────
    def menu(self, heading, items: list, extra: Optional[ft.Control] = None):
        holder: dict = {}

        def row(icon, label, fn, danger=False, rot=0):
            def go(e):
                self.app.close_dialog(holder["dlg"])
                fn()
            c = DANGER if danger else MAJOR
            return ft.Container(ft.Row([ico(icon, 20, c, rot), text(label, 14, c)], spacing=12), padding=ft.Padding.symmetric(vertical=11, horizontal=6),
                                ink=True, border_radius=8, on_click=go)
        col = ft.Column(([extra] if extra else []) + [row(*it[:3], *(it[3:])) if len(it) > 3 else row(*it) for it in items],
                        spacing=0, tight=True, width=250)
        holder["dlg"] = self.app.dialog(heading, col)

    def add_menu(self):
        self.menu("New layer", [("pencil", "Shapes", lambda: self.add_layer("shapes")),
                                ("brush", "Paint", lambda: self.add_layer("paint")),
                                ("picture", "Image", self.add_image_layer)])

    def add_layer(self, kind):
        def fn():
            self.doc.add_layer(kind)
            self.sel = len(self.doc.layers) - 1
        self.commit_op(fn)

    def add_image_layer(self):
        """Images come in through the normal import dialog, which works on the blueprint: save, go back, import."""
        if not self.leave_commit():
            return
        self.app.pop()
        if self.bp_view:
            self.app.spawn(self.bp_view.on_pick_layer(lyr.NEW))

    def layer_menu(self):
        l = self.layer
        if not l:
            return
        name = ft.TextField(value=l.name, dense=True, border_color=SEP, on_change=lambda e: self.rename(e.control.value),
                            text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
        items = [("left_arrow", "Move up", lambda: self.move_layer(1), False, 90), ("left_arrow", "Move down", lambda: self.move_layer(-1), False, -90)]
        if l.type == "shapes":
            items.append(("brush", "Convert to paint", lambda: self.commit_op(lambda: art.convert_to_paint(self.doc, l))))
        if self.sel > 0:
            items.append(("merge", "Merge down", self.merge_down))
        items.append(("delete", "Delete layer", self.delete_layer, True))
        self.menu(None, items, extra=name)

    def rename(self, v: str):
        l = self.layer
        if l:
            l.name = v
            self.changed = True

    def move_layer(self, d):
        i, j = self.sel, self.sel + d
        if 0 <= j < len(self.doc.layers):
            def fn():
                L = self.doc.layers
                L[i], L[j] = L[j], L[i]
                self.sel = j
            self.commit_op(fn)

    def delete_layer(self):
        if len(self.doc.layers) <= 1:
            self.app.toast("A drawing needs at least one layer.", error=True)
            return

        def fn():
            del self.doc.layers[self.sel]
        self.commit_op(fn)

    def merge_down(self):
        i, n = self.sel, art.fill_conflicts(self.doc, self.sel)

        def do(clear):
            def fn():
                art.merge_down(self.doc, i, clear)
                self.sel = i - 1
            self.commit_op(fn)
        if n:
            self.app.confirm("Merge down", f"{n} frame(s) have buckets in both layers. Merging removes all bucketing in the merged layer. Continue?",
                             "Remove buckets and merge", lambda: do(True))
        else:
            do(False)

    def cell_menu(self):
        l = self.layer
        if not l:
            return
        items = []
        if self.cf:
            linked = self.doc.linked(l, self.cf)
            items.append(("unlink" if linked else "link", "Unlink from previous" if linked else "Link to previous", self.toggle_link))
        items.append(("copy", "Copy", self.copy_cell))
        if self.clip and self.clip[0] == l.type:
            items.append(("paste", "Paste", self.paste_cell))
        self.menu(None, items)

    def toggle_link(self):
        l, f = self.layer, self.cf

        def fn():
            old = l.fr[f]
            if self.doc.linked(l, f):
                n = old.copy()
                if isinstance(n, art.PaintData):
                    n.own()
                for i in range(f, self.doc.F):
                    if l.fr[i] is old:
                        l.fr[i] = n
            else:
                n = l.fr[f - 1]
                for i in range(f, self.doc.F):
                    if l.fr[i] is old:
                        l.fr[i] = n
        self.commit_op(fn)

    def copy_cell(self):
        c = self.cur()
        if c:
            d = c[1].copy()
            if isinstance(d, art.PaintData):
                d.px = d.px.copy()
                d.shared = False
            self.clip = (c[0].type, d)

    def paste_cell(self):
        l, f = self.layer, self.cf
        if not (l and self.clip and self.clip[0] == l.type):
            return

        def fn():
            old = l.fr[f]
            n = self.clip[1].copy()
            if isinstance(n, art.PaintData):
                n.own()
            if isinstance(n, art.ShapeData):
                n.touch()
            for i in range(f, self.doc.F):
                if l.fr[i] is old:
                    l.fr[i] = n
        self.commit_op(fn)

    def add_frame(self):
        def fn():
            self.doc.F += 1
            for l in self.doc.layers:
                l.fr.append(art.new_data(l.type, self.doc.W, self.doc.H))
            self.cf = self.doc.F - 1
        self.commit_op(fn)

    def frame_menu(self):
        items = [("left_arrow", "Move left", lambda: self.move_frame(-1)), ("left_arrow", "Move right", lambda: self.move_frame(1), False, 180)]
        items.append(("delete", "Delete frame", self.delete_frame, True))
        self.menu(None, items)

    def move_frame(self, d):
        f, j = self.cf, self.cf + d
        if 0 <= j < self.doc.F:
            def fn():
                for l in self.doc.layers:
                    l.fr[f], l.fr[j] = l.fr[j], l.fr[f]
                self.cf = j
            self.commit_op(fn)

    def delete_frame(self):
        if self.doc.F <= 1:
            return
        f = self.cf

        def fn():
            for l in self.doc.layers:
                del l.fr[f]
            self.doc.F -= 1
            self.cf = min(f, self.doc.F - 1)
        self.commit_op(fn)

    # ── leaving ───────────────────────────────────────────────────────────
    def leave_commit(self) -> bool:
        """Write the drawing into the blueprint (one undo step of the card). Returns False if that failed."""
        if not self.changed:
            return True
        try:
            dd = art.to_draw(self.doc)
            self.ctx.edit_sync(lambda: ops.set_draw_data(self.bp_path, dd))
        except Exception as ex:
            self.app.log(f"error: art editor save: {ex}")
            self.app.toast(f"Couldn't save the drawing: {ex}", error=True)
            return False
        self.changed = False
        return True

    def on_leave(self):
        self.alive = False
        self.leave_commit()


def bar(app, ed: ArtEditor) -> ft.Control:
    return ft.Container(ft.Row([
        ft.Container(icon_button(castle_icon("left_arrow", 22), lambda e: app.go_back(), "Back"), width=56, alignment=ft.Alignment.CENTER_LEFT),
        ft.Container(expand=True),
        icon_button(castle_icon("undo", 22), ed.do_undo, "Undo", disabled=not ed.undo),
        icon_button(castle_icon("redo", 22), ed.do_redo, "Redo", disabled=not ed.redo),
        ft.Container(width=16),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=0), padding=ft.Padding(left=6, right=6, top=4, bottom=4))


def art_screen(app, ctx, bp_path: Path, d2: dict, view=None) -> Screen:
    doc = art.load_castle(d2.get("drawData") or {})
    ed = ArtEditor(app, ctx, bp_path, doc, view)
    return Screen(title="", build=lambda a: ed.root, bar=lambda a: bar(a, ed), on_leave=ed.on_leave, title_font=False,
                  sig=lambda a: (bool(ed.undo), bool(ed.redo)))
