"""Piano roller for the Music tab: a read-only, horizontally scrolling view of the whole song.

Notes are plain squares in the color of their track's pattern. The needle follows the song; drag its
handle to scrub, drag anywhere else to scroll. Play/stop and follow-needle sit inside the box (top right).
The picture is drawn with PIL (one small PNG per frame) and shown in a single ft.Image.
"""
from __future__ import annotations

import asyncio
import io
import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Callable, Optional

import flet as ft
from PIL import Image, ImageDraw

import synth
from theme import BG, MAJOR, MINOR1, castle_icon

SM = 11                 # side margin: the needle handle may poke out of the box
HM = 14                 # top margin: the handle sits half outside the box
RATIO = 1.4             # box height / width (7:5)
MAX_CELL = 48.0
BLACK_KEYS = {1, 3, 6, 8, 10}
GRAY = (136, 136, 136)


@dataclass
class RollerModel:
    steps: list             # note start (in steps), ascending
    rows: list              # row of every note
    tracks: list            # track index of every note
    colors: list            # (r, g, b) per track
    hi: int                 # highest tonal key
    tonal: int              # number of tonal rows (min..max key only)
    has_drums: bool
    row_count: int
    end: float              # step of the last note
    sec: float              # seconds per step

    def cell(self, box_h: float) -> float:
        return min(box_h / max(1, self.row_count), MAX_CELL)


def _rgb(hex_: str) -> tuple:
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))


def build_model(music: dict, bpm: float) -> Optional[RollerModel]:
    notes = synth.collect_notes(music, bpm)
    if not notes:
        return None
    voices, _ = synth.voices_from_music(music)
    sec = synth.step_seconds(bpm)
    colors = [_rgb(c) for c in synth.track_colors(music)]
    hi, lo, dhi, dlo, has_drums = -1e9, 1e9, -1, 99, False
    keys = []
    for n in notes:
        ti = n["track"]
        if ti < len(voices) and voices[ti].get("drums"):
            has_drums = True
            k = max(36, min(42, round(n["key"])))
            dlo, dhi = min(dlo, k), max(dhi, k)
            keys.append((True, k))
        else:
            k = round(n["key"])
            lo, hi = min(lo, k), max(hi, k)
            keys.append((False, k))
    tonal = int(hi - lo + 1) if hi >= lo else 0
    drum_top = tonal + (1 if tonal and has_drums else 0)
    rows = [(drum_top + (dhi - k)) if drum else int(hi - k) for drum, k in keys]
    return RollerModel(
        steps=[n["t"] / sec for n in notes], rows=rows, tracks=[n["track"] for n in notes], colors=colors,
        hi=int(hi) if tonal else 0, tonal=tonal, has_drums=has_drums,
        row_count=(drum_top + (dhi - dlo + 1)) if has_drums else tonal, end=notes[-1]["t"] / sec, sec=sec)


# ── drawing ───────────────────────────────────────────────────────────────

_MASKS: dict = {}


def _mask(w: int, h: int, r: int) -> Image.Image:
    key = (w, h, r)
    m = _MASKS.get(key)
    if m is None:
        if len(_MASKS) > 8:
            _MASKS.clear()
        m = Image.new("L", (w, h), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
        _MASKS[key] = m
    return m


def _rounded_poly(pts: list, r: float, n: int = 6) -> list:
    """Corners of a polygon cut with quadratic curves (the rounded needle handle)."""
    out = []
    k = len(pts)
    for i in range(k):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % k]
        la, lc = math.dist(a, b), math.dist(b, c)
        da, dc = min(r, la * 0.45), min(r, lc * 0.45)
        p1 = (b[0] + (a[0] - b[0]) * da / la, b[1] + (a[1] - b[1]) * da / la)
        p2 = (b[0] + (c[0] - b[0]) * dc / lc, b[1] + (c[1] - b[1]) * dc / lc)
        for j in range(n + 1):
            t = j / n
            u = 1 - t
            out.append((u * u * p1[0] + 2 * u * t * b[0] + t * t * p2[0],
                        u * u * p1[1] + 2 * u * t * b[1] + t * t * p2[1]))
    return out


def max_scroll(m: RollerModel, box_w: float, box_h: float) -> float:
    return max(0.0, (m.end + 1) * m.cell(box_h) - box_w * 0.5)


def render_png(m: RollerModel, box_w: float, box_h: float, ox: float, needle: float, sc: int = 2) -> bytes:
    """One frame. The image is (box_w + 2*SM) x (box_h + HM) logical px, at `sc` device px per logical px."""
    c = m.cell(box_h)
    bw, bh = round(box_w * sc), round(box_h * sc)
    layer = Image.new("RGB", (bw, bh), (0, 0, 0))
    d = ImageDraw.Draw(layer)
    yo = (box_h - m.row_count * c) / 2
    X = lambda s: s * c - ox
    Y = lambda r: yo + r * c

    def hline(y: float, g: int):
        yy = round(y * sc)
        d.rectangle([0, yy, bw - 1, yy + sc - 1], fill=(g, g, g))

    def vline(x: float, g: int):
        xx = round(x * sc)
        d.rectangle([xx, 0, xx + sc - 1, bh - 1], fill=(g, g, g))

    pc = lambda r: ((m.hi - r - 12) % 12 + 12) % 12
    for r in range(m.tonal):                                   # black-key rows
        if pc(r) in BLACK_KEYS:
            d.rectangle([0, round(Y(r) * sc), bw - 1, round(Y(r + 1) * sc) - 1], fill=(14, 14, 14))
    for r in range(1, m.tonal):                                # octave lines (top edge of each C)
        if pc(r) == 0:
            hline(Y(r), 95)
    if m.has_drums and m.tonal:
        hline(Y(m.tonal + 0.5), 95)
    a0, a1 = max(0, math.floor(ox / c)), math.ceil((ox + box_w) / c)
    for every, g, on in ((1, 14, c >= 8), (4, 30, c * 4 >= 10), (16, 61, True)):
        if on:
            for s in range(math.ceil(a0 / every) * every, a1 + 1, every):
                vline(X(s), g)
    sz = max(2.0, c - 1)                                       # notes: plain squares, track color
    for i in range(bisect_left(m.steps, a0 - 1), len(m.steps)):
        s = m.steps[i]
        if s > a1:
            break
        x, y = X(s), Y(m.rows[i])
        d.rectangle([round(x * sc), round(y * sc), max(round(x * sc), round((x + sz) * sc) - 1),
                     max(round(y * sc), round((y + sz) * sc) - 1)], fill=m.colors[m.tracks[i]])
    nx = X(needle)
    x0 = round((nx - 0.75) * sc)
    d.rectangle([x0, round(10 * sc), max(x0 + 1, round((nx + 0.75) * sc)), bh - 1], fill=(255, 255, 255))

    out = Image.new("RGBA", (round((box_w + 2 * SM) * sc), round((box_h + HM) * sc)), (0, 0, 0, 0))
    out.paste(layer, (SM * sc, HM * sc), _mask(bw, bh, 11 * sc))
    od = ImageDraw.Draw(out)
    od.rounded_rectangle([SM * sc, HM * sc, SM * sc + bw - 1, HM * sc + bh - 1], radius=12 * sc,
                         outline=GRAY, width=sc)
    ax = SM + nx                                               # handle: half outside the box, tip on the needle
    if -SM <= nx <= box_w + SM:
        pts = [(ax - 11, HM - 13), (ax + 11, HM - 13), (ax + 11, HM), (ax, HM + 13), (ax - 11, HM)]
        od.polygon([(x * sc, y * sc) for x, y in _rounded_poly(pts, 4)], fill=(255, 255, 255))
    buf = io.BytesIO()
    out.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


# ── control ───────────────────────────────────────────────────────────────

class PianoRoller:
    """Roller + in-box play/stop and follow buttons, driven by a live_synth.LiveSynth."""

    def __init__(self, app, eng, model: RollerModel, width: float, on_error: Callable[[str], None]):
        self.app, self.eng, self.m = app, eng, model
        self.on_error = on_error
        self.bw = max(140.0, width - 2 * SM)
        self.bh = self.bw / RATIO
        self.ox = 0.0
        self.needle = 0.0                          # steps
        self.follow = True
        self.mode: Optional[str] = None            # None | "pan" | "needle"
        self.was_playing = False
        self.last_x = 0.0
        self.dirty = True
        self._shown_playing: Optional[bool] = None
        self._shown_follow: Optional[bool] = None
        self.img = ft.Image(src=render_png(model, self.bw, self.bh, 0, 0), width=self.bw + 2 * SM,
                            height=self.bh + HM, fit=ft.BoxFit.FILL, gapless_playback=True)
        self.play_btn = self._button("play_music", self.toggle, HM + 8)
        self.follow_btn = self._button("follow_needle", self.toggle_follow, HM + 56)
        self.control = ft.Stack([
            ft.GestureDetector(content=self.img, on_pan_start=self._down, on_pan_update=self._move,
                               on_pan_end=self._up, on_pan_cancel=self._up, drag_interval=20),
            self.play_btn, self.follow_btn,
        ], width=self.bw + 2 * SM, height=self.bh + HM)
        self._paint_buttons()

    def _button(self, icon: str, on_click, top: float):
        return ft.Container(content=castle_icon(icon, 20), width=40, height=40, border_radius=12,
                            alignment=ft.Alignment.CENTER, left=SM + self.bw - 48, top=top, on_click=on_click)

    def _paint_buttons(self):
        playing = self.eng.playing
        if playing != self._shown_playing:
            self._shown_playing = playing
            on = playing
            self.play_btn.content = castle_icon("stop" if playing else "play_music", 20, BG if on else MINOR1)
            self.play_btn.bgcolor = MAJOR if on else BG
            self.play_btn.border = ft.Border.all(1, MAJOR if on else MINOR1)
        if self.follow != self._shown_follow:
            self._shown_follow = self.follow
            self.follow_btn.content = castle_icon("follow_needle", 20, BG if self.follow else MINOR1)
            self.follow_btn.bgcolor = MAJOR if self.follow else BG
            self.follow_btn.border = ft.Border.all(1, MAJOR if self.follow else MINOR1)

    # transport -------------------------------------------------------------
    def toggle(self, e=None):
        if self.eng.playing:
            self.eng.stop()                        # stop = back to the start
            self.needle, self.ox = 0.0, 0.0
        else:
            self.eng.play()
        self.dirty = True
        self._frame()

    def toggle_follow(self, e=None):
        self.follow = not self.follow
        self.dirty = True
        self._frame()

    # input -------------------------------------------------------------------
    def _handle_hit(self, x: float, y: float) -> bool:
        nx = SM + self.needle * self.m.cell(self.bh) - self.ox
        return abs(x - nx) <= 22 and HM - 26 <= y <= HM + 20 and 0 <= x <= self.bw + 2 * SM

    def _needle_to(self, x: float):
        c = self.m.cell(self.bh)
        self.needle = max(0.0, min(self.m.end + 1, (x - SM + self.ox) / c))
        self.last_x = x
        self.dirty = True

    def _down(self, e):
        x, y = e.local_position.x, e.local_position.y
        self.last_x = x
        if self._handle_hit(x, y):
            self.mode = "needle"
            self.was_playing = self.eng.playing
            if self.was_playing:
                self.eng.pause()
            self._needle_to(x)
        else:
            self.mode = "pan"
        self._frame()

    def _move(self, e):
        x = e.local_position.x
        if self.mode == "needle":
            self._needle_to(x)
        elif self.mode == "pan":
            dx = x - self.last_x
            self.ox = max(0.0, min(max_scroll(self.m, self.bw, self.bh), self.ox - dx))
            if self.eng.playing and self.follow and abs(dx) > 2:
                self.follow = False
            self.last_x = x
            self.dirty = True
        self._frame()

    def _up(self, e=None):
        if self.mode == "needle":
            self.eng.seek(self.needle * self.m.sec)
            if self.was_playing:
                self.eng.play()
        self.mode = None
        self._frame()

    # drawing -----------------------------------------------------------------
    def _frame(self):
        self._paint_buttons()
        self.img.src = render_png(self.m, self.bw, self.bh, self.ox, self.needle)
        self.dirty = False
        self.app.page.update()

    async def run(self, alive: Callable[[], bool]):
        """Follows the engine while the Music tab is open."""
        m, eng = self.m, self.eng
        shown_error, was_playing = False, False
        while alive():
            if eng.error and not shown_error:
                shown_error = True
                self.on_error(eng.error)
            playing = eng.playing
            if self.mode == "needle":                         # edge auto-scroll while scrubbing
                lim = max_scroll(m, self.bw, self.bh)
                if self.last_x > SM + self.bw - 28:
                    self.ox = min(lim, self.ox + 8)
                    self._needle_to(self.last_x)
                elif self.last_x < SM + 28 and self.ox > 0:
                    self.ox = max(0.0, self.ox - 8)
                    self._needle_to(self.last_x)
            elif playing:
                self.needle = eng.position / m.sec
                if self.follow:
                    c = m.cell(self.bh)
                    lim, nx = self.bw * 0.4, self.needle * c - self.ox
                    if nx > lim or nx < 0:
                        self.ox = max(0.0, min(max_scroll(m, self.bw, self.bh), self.needle * c - lim))
                self.dirty = True
            elif was_playing:                                  # song ended by itself: back to the start
                eng.stop()
                self.needle, self.ox = 0.0, 0.0
                self.dirty = True
            was_playing = playing
            if self.dirty or playing != self._shown_playing:
                self._frame()
            await asyncio.sleep(0.06 if (playing or self.mode) else 0.15)
