"""Reusable controls: number box (digits only, wheel + circled +/- buttons), color field
(swatch + hex, tap for a picker), the HSV wheel color picker, and the key-info note."""
from __future__ import annotations

import colorsys
import functools
import io
import math
import re
from typing import Callable, Optional

import flet as ft

import theme
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, minor, text

FIELD_TEXT = ft.TextStyle(font_family=theme.BODY, color=MAJOR, size=14)


def icon_button(icon: ft.Control, on_click=None, tooltip: Optional[str] = None, size: int = 40,
                disabled: bool = False) -> ft.Container:
    return ft.Container(
        content=icon, width=size, height=size, alignment=ft.Alignment.CENTER,
        border_radius=size / 2, ink=not disabled, on_click=None if disabled else on_click,
        tooltip=tooltip, opacity=0.3 if disabled else 1.0,
    )


def circle_button(icon: ft.Control, on_click=None, size: int = 30, disabled: bool = False) -> ft.Container:
    """Icon inside a thin circular outline (Castle's +/- style)."""
    return ft.Container(
        content=icon, width=size, height=size, alignment=ft.Alignment.CENTER,
        border_radius=size / 2, border=ft.Border.all(1, SEP), ink=not disabled,
        on_click=None if disabled else on_click, opacity=0.3 if disabled else 1.0,
    )


def pill_button(label: str, on_click, icon: Optional[ft.Control] = None, disabled: bool = False,
                filled: bool = False, busy: bool = False, width: Optional[float] = None,
                color: Optional[str] = None) -> ft.Container:
    """Monochrome button. Filled = white with black text. Disabled filled = separator gray.
    `busy` shows a throbber in place of the label. `color` overrides the label color (outlined only)."""
    if filled:
        fg = BG
        bg = SEP if (disabled and not busy) else MAJOR
    else:
        fg, bg = color or MAJOR, None
    if busy:
        inner: list[ft.Control] = [ft.ProgressRing(width=16, height=16, stroke_width=2, color=fg)]
    else:
        inner = ([icon] if icon else []) + [text(label, 14, fg, medium=True)]
    off = disabled or busy
    return ft.Container(
        content=ft.Row(inner, spacing=8, tight=True, alignment=ft.MainAxisAlignment.CENTER),
        padding=ft.Padding.symmetric(vertical=9, horizontal=18), width=width,
        border_radius=22, ink=not off, bgcolor=bg,
        border=None if filled else ft.Border.all(1, SEP),
        opacity=0.5 if (disabled and not filled) else 1.0,
        on_click=None if off else on_click, alignment=ft.Alignment.CENTER,
    )


# ── info ───────────────────────────────────────────────────────────────────

def info_note(message: str, size: float = 12) -> ft.Control:
    """Always-visible hint: key-info.png to the left of the text (as in the Castle app)."""
    img = ft.Image(src="icons/key-info.png", width=16, height=16, fit=ft.BoxFit.CONTAIN,
                   error_content=ft.Text("i", size=12, color=MINOR1))
    return ft.Row([ft.Container(img, padding=ft.Padding(top=1)), minor(message, size, expand=True)],
                  spacing=8, vertical_alignment=ft.CrossAxisAlignment.START)


# ── number box ─────────────────────────────────────────────────────────────

class NumberBox:
    """Digits-only TextField with circled +/- buttons and mouse-wheel stepping.
    `.control` is the thing to add to a layout; `.value` is the current text."""

    def __init__(self, label: Optional[str], value, on_change: Callable[[str], None], step: float = 1,
                 minimum: Optional[float] = None, maximum: Optional[float] = None,
                 decimals: int = 0, width: int = 170, enabled: bool = True):
        self.step, self.min, self.max, self.decimals = step, minimum, maximum, decimals
        self.on_change = on_change
        regex = r"^[0-9]*\.?[0-9]*$" if decimals else r"^[0-9]*$"
        self.field = ft.TextField(
            value=self._fmt(value), label=label or None, dense=True, border_color=SEP, text_style=FIELD_TEXT,
            keyboard_type=ft.KeyboardType.NUMBER, input_filter=ft.InputFilter(regex_string=regex, allow=True),
            on_change=self._typed, expand=True, disabled=not enabled,
            label_style=ft.TextStyle(font_family=theme.BODY, color=MINOR1, size=12))
        minus = circle_button(castle_icon("horizontal_line", 16), lambda e: self._bump(-1), disabled=not enabled)
        plus = circle_button(castle_icon("add", 16), lambda e: self._bump(1), disabled=not enabled)
        self.control = ft.GestureDetector(
            content=ft.Row([self.field, minus, plus], spacing=6, width=width,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
            on_scroll=self._wheel)

    @property
    def value(self) -> str:
        return self.field.value or ""

    def _fmt(self, v) -> str:
        if isinstance(v, (int, float)):
            return f"{v:g}"
        return str(v)

    def _number(self) -> float:
        try:
            return float(self.value)
        except ValueError:
            return float(self.min if self.min is not None else 0)

    def _clamp(self, v: float) -> float:
        if self.min is not None:
            v = max(self.min, v)
        if self.max is not None:
            v = min(self.max, v)
        return v

    def _typed(self, e):
        self.on_change(self.value)

    def _bump(self, direction: int):
        v = self._clamp(round(self._number() + direction * self.step, max(self.decimals, 6)))
        self.field.value = self._fmt(int(v) if not self.decimals else v)
        self.field.update()
        self.on_change(self.value)

    def _wheel(self, e):
        dy = getattr(getattr(e, "scroll_delta", None), "y", 0) or 0
        if dy:
            self._bump(-1 if dy > 0 else 1)   # wheel up = increase


# ── color ──────────────────────────────────────────────────────────────────

def norm_hex(value: str, default: str = "#000000") -> str:
    t = (value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", t):
        t = "".join(c * 2 for c in t)
    return "#" + t.lower() if re.fullmatch(r"[0-9a-fA-F]{6}", t) else default


class ColorField:
    """Swatch with the hex number to its right. Tap for a picker.
    `enabled=False` grays it out and ignores taps."""

    def __init__(self, app, value: str, on_change: Callable[[str], None], enabled: bool = True):
        self.app, self.on_change, self.enabled = app, on_change, enabled
        self.value = norm_hex(value)
        self.swatch = ft.Container(width=30, height=30, border_radius=8, bgcolor=self.value,
                                   border=ft.Border.all(1, SEP))
        self.label = text(self.value, 14, MAJOR)
        self.control = ft.Container(
            ft.Row([self.swatch, self.label], spacing=10, tight=True),
            padding=ft.Padding.symmetric(vertical=4), ink=enabled, border_radius=8,
            opacity=1.0 if enabled else 0.35, on_click=self._open if enabled else None)

    def set(self, value: str):
        self.value = norm_hex(value, self.value)
        self.swatch.bgcolor = self.value
        self.label.value = self.value
        self.app.page.update()
        self.on_change(self.value)

    def _open(self, e):
        open_color_picker(self.app, self.value, self.set)


# ── HSV wheel picker ───────────────────────────────────────────────────────

WHEEL = 240            # picker square
RING = 24              # hue ring thickness
DIAMOND = 120          # side of the (rotated) saturation/value square
KNOB = 24              # slider knob diameter
TRACK_W = 200          # slider track width


def _upd(control) -> None:
    try:
        control.update()
    except RuntimeError:        # not on a page (headless tests)
        pass


def _hsv_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, max(0, min(1, s)), max(0, min(1, v)))
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


@functools.lru_cache(maxsize=1)
def hue_ring_png() -> bytes:
    """Anti-aliased hue ring (0° at 3 o'clock, increasing clockwise), transparent inside."""
    import numpy as np
    from PIL import Image
    ss = 3
    n = WHEEL * ss
    c = (n - 1) / 2
    yy, xx = np.mgrid[0:n, 0:n].astype("float32")
    dx, dy = xx - c, yy - c
    r = np.hypot(dx, dy)
    hue = (np.degrees(np.arctan2(dy, dx)) % 360) / 60.0
    i = np.floor(hue).astype(int) % 6
    f = hue - np.floor(hue)
    q, t = 1 - f, f
    zero, one = np.zeros_like(f), np.ones_like(f)
    rgb = np.stack([np.choose(i, [one, q, zero, zero, t, one]),
                    np.choose(i, [t, one, one, q, zero, zero]),
                    np.choose(i, [zero, zero, t, one, one, q])], axis=-1)
    r_out, r_in = WHEEL * ss / 2, (WHEEL - 2 * RING) * ss / 2
    alpha = np.clip(r_out - r, 0, 1) * np.clip(r - r_in, 0, 1)
    img = np.dstack([(rgb * 255).astype("uint8"), (alpha * 255).astype("uint8")])
    out = Image.fromarray(img, "RGBA").resize((WHEEL, WHEEL), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


class GradientSlider:
    """Slider with a colored track and a plain white knob."""

    def __init__(self, label: str, maximum: float, value: float, on_change: Callable[[float], None],
                 colors: list[str]):
        self.max, self.value, self.on_change = maximum, value, on_change
        self.track = ft.Container(top=(KNOB - 14) / 2, left=KNOB / 2, width=TRACK_W, height=14, border_radius=7,
                                  gradient=ft.LinearGradient(begin=ft.Alignment.CENTER_LEFT,
                                                             end=ft.Alignment.CENTER_RIGHT, colors=colors),
                                  border=ft.Border.all(1, SEP))
        self.knob = ft.Container(width=KNOB, height=KNOB, border_radius=KNOB / 2, bgcolor="#ffffff", top=0,
                                 left=self._left(), shadow=ft.BoxShadow(blur_radius=3, color="#99000000"))
        self.readout = text(self._shown(), 12, MINOR1, text_align=ft.TextAlign.RIGHT)
        area = ft.GestureDetector(
            content=ft.Stack([self.track, self.knob], width=TRACK_W + KNOB, height=KNOB),
            on_tap_down=self._touch, on_pan_start=self._touch, on_pan_update=self._touch, drag_interval=20)
        self.control = ft.Row([
            ft.Container(text(label, 13, MINOR1), width=14),
            area,
            ft.Container(self.readout, width=34, alignment=ft.Alignment.CENTER_RIGHT),
        ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    def _left(self) -> float:
        return (self.value / self.max) * TRACK_W if self.max else 0

    def _shown(self) -> str:
        return str(round(self.value))

    def _touch(self, e):
        x = e.local_position.x - KNOB / 2
        self.value = max(0.0, min(1.0, x / TRACK_W)) * self.max
        self.paint()
        self.on_change(self.value)

    def paint(self):
        self.knob.left = self._left()
        self.readout.value = self._shown()

    def set(self, value: float, colors: Optional[list[str]] = None):
        self.value = max(0.0, min(self.max, value))
        if colors:
            self.track.gradient = ft.LinearGradient(begin=ft.Alignment.CENTER_LEFT,
                                                    end=ft.Alignment.CENTER_RIGHT, colors=colors)
        self.paint()


def open_color_picker(app, current: str, on_pick: Callable[[str], None]):
    r0, g0, b0 = (int(norm_hex(current)[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h0, s0, v0 = colorsys.rgb_to_hsv(r0, g0, b0)
    st = {"h": h0 * 360, "s": s0 * 100, "v": v0 * 100, "tab": "rgb", "grab": None}

    def rgb() -> tuple[float, float, float]:
        r, g, b = colorsys.hsv_to_rgb(st["h"] / 360, st["s"] / 100, st["v"] / 100)
        return r * 255, g * 255, b * 255

    def hex_now() -> str:
        return _hsv_hex(st["h"], st["s"] / 100, st["v"] / 100)

    # ── top row: close (left) + square swatch (right) ──
    swatch = ft.Container(width=36, height=36, border_radius=6, bgcolor=hex_now(), border=ft.Border.all(1, SEP))
    close = icon_button(castle_icon("close", 22), lambda e: app.page.pop_dialog(), size=36)

    # ── wheel ──
    ring_img = ft.Image(src=hue_ring_png(), width=WHEEL, height=WHEEL, fit=ft.BoxFit.CONTAIN,
                        gapless_playback=True)
    sat_layer = ft.Container(width=DIAMOND, height=DIAMOND, gradient=ft.LinearGradient(
        begin=ft.Alignment.CENTER_LEFT, end=ft.Alignment.CENTER_RIGHT, colors=["#ffffff", _hsv_hex(st["h"], 1, 1)]))
    val_layer = ft.Container(width=DIAMOND, height=DIAMOND, gradient=ft.LinearGradient(
        begin=ft.Alignment.TOP_CENTER, end=ft.Alignment.BOTTOM_CENTER, colors=["#00000000", "#000000"]))
    off = (WHEEL - DIAMOND) / 2
    diamond = ft.Container(ft.Stack([sat_layer, val_layer]), width=DIAMOND, height=DIAMOND, left=off, top=off,
                           rotate=ft.Rotate(angle=math.pi / 4), clip_behavior=ft.ClipBehavior.ANTI_ALIAS)
    ring_mark = ft.Container(width=18, height=18, border_radius=9, border=ft.Border.all(2, "#ffffff"),
                             shadow=ft.BoxShadow(blur_radius=3, color="#99000000"))
    sv_mark = ft.Container(width=18, height=18, border_radius=9, border=ft.Border.all(2, "#ffffff"),
                           shadow=ft.BoxShadow(blur_radius=3, color="#99000000"))

    def place_marks():
        mid = WHEEL / 2 - RING / 2
        a = math.radians(st["h"])
        ring_mark.left = WHEEL / 2 + mid * math.cos(a) - 9
        ring_mark.top = WHEEL / 2 + mid * math.sin(a) - 9
        ring_mark.bgcolor = _hsv_hex(st["h"], 1, 1)
        # (s, v) → position inside the unrotated square, then rotate 45° clockwise about the centre
        ux = (st["s"] / 100 - 0.5) * DIAMOND
        uy = (0.5 - st["v"] / 100) * DIAMOND
        c, sn = math.cos(math.pi / 4), math.sin(math.pi / 4)
        sx, sy = ux * c - uy * sn, ux * sn + uy * c
        sv_mark.left = WHEEL / 2 + sx - 9
        sv_mark.top = WHEEL / 2 + sy - 9
        sv_mark.bgcolor = hex_now()

    def wheel_touch(e):
        dx, dy = e.local_position.x - WHEEL / 2, e.local_position.y - WHEEL / 2
        if st["grab"] is None:
            st["grab"] = "ring" if math.hypot(dx, dy) >= WHEEL / 2 - RING - 6 else "sv"
        if st["grab"] == "ring":
            st["h"] = math.degrees(math.atan2(dy, dx)) % 360
        else:
            c, sn = math.cos(math.pi / 4), math.sin(math.pi / 4)
            ux, uy = dx * c + dy * sn, -dx * sn + dy * c          # undo the 45° rotation
            st["s"] = max(0.0, min(1.0, ux / DIAMOND + 0.5)) * 100
            st["v"] = max(0.0, min(1.0, 0.5 - uy / DIAMOND)) * 100
        sync()

    def wheel_release(e):
        st["grab"] = None

    wheel = ft.GestureDetector(
        content=ft.Stack([ring_img, diamond, ring_mark, sv_mark], width=WHEEL, height=WHEEL),
        on_tap_down=wheel_touch, on_pan_start=wheel_touch, on_pan_update=wheel_touch,
        on_tap_up=wheel_release, on_pan_end=wheel_release, on_pan_cancel=lambda e: wheel_release(e),
        drag_interval=25)

    # ── sliders ──
    HUES = [_hsv_hex(h, 1, 1) for h in range(0, 361, 60)]

    def set_rgb(r: float, g: float, b: float):
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s > 0 and v > 0:            # grays have no hue: keep the one the user had
            st["h"] = h * 360
        st["s"], st["v"] = s * 100, v * 100
        sync()

    def rgb_changed(idx: int):
        def f(val: float):
            c = list(rgb())
            c[idx] = val
            set_rgb(*c)
        return f

    def hsv_changed(key: str):
        def f(val: float):
            st[key] = val
            sync()
        return f

    r_s = GradientSlider("R", 255, rgb()[0], rgb_changed(0), ["#000000", "#ff0000"])
    g_s = GradientSlider("G", 255, rgb()[1], rgb_changed(1), ["#000000", "#00ff00"])
    b_s = GradientSlider("B", 255, rgb()[2], rgb_changed(2), ["#000000", "#0000ff"])
    h_s = GradientSlider("H", 360, st["h"], hsv_changed("h"), HUES)
    s_s = GradientSlider("S", 100, st["s"], hsv_changed("s"), [_hsv_hex(st["h"], 0, st["v"] / 100),
                                                               _hsv_hex(st["h"], 1, st["v"] / 100)])
    v_s = GradientSlider("V", 100, st["v"], hsv_changed("v"), ["#000000", "#ffffff"])
    slider_host = ft.Column(spacing=8, tight=True)
    tab_host = ft.Row(spacing=0)

    def paint_tabs():
        def tab(key: str, label: str):
            on = st["tab"] == key

            def go(e, k=key):
                st["tab"] = k
                paint_tabs()
                _upd(body)
            return ft.Container(text(label, 13, MAJOR if on else MINOR1, bold=on), expand=True,
                                alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(vertical=8), ink=True,
                                on_click=go, border=ft.Border(bottom=ft.BorderSide(2 if on else 1, MAJOR if on else SEP)))
        tab_host.controls = [tab("rgb", "RGB"), tab("hsv", "HSV")]
        slider_host.controls = ([r_s.control, g_s.control, b_s.control] if st["tab"] == "rgb"
                                else [h_s.control, s_s.control, v_s.control])

    # ── bottom row: hex (left) + OK (right) ──
    hex_field = ft.TextField(value=hex_now(), dense=True, border_color=SEP, text_style=FIELD_TEXT, width=118,
                             input_filter=ft.InputFilter(regex_string=r"^#?[0-9a-fA-F]{0,6}$", allow=True))

    def sync(from_hex: bool = False):
        c = hex_now()
        swatch.bgcolor = c
        if not from_hex:
            hex_field.value = c
        sat_layer.gradient = ft.LinearGradient(begin=ft.Alignment.CENTER_LEFT, end=ft.Alignment.CENTER_RIGHT,
                                               colors=["#ffffff", _hsv_hex(st["h"], 1, 1)])
        r, g, b = rgb()
        r_s.set(r)
        g_s.set(g)
        b_s.set(b)
        h_s.set(st["h"])
        s_s.set(st["s"], [_hsv_hex(st["h"], 0, st["v"] / 100), _hsv_hex(st["h"], 1, st["v"] / 100)])
        v_s.set(st["v"])
        place_marks()
        _upd(body)

    def typed(e):
        c = norm_hex(hex_field.value, "")
        if c:
            rr, gg, bb = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
            hh, ss, vv = colorsys.rgb_to_hsv(rr, gg, bb)
            if ss > 0 and vv > 0:
                st["h"] = hh * 360
            st["s"], st["v"] = ss * 100, vv * 100
            sync(from_hex=True)

    hex_field.on_change = typed

    def ok(e):
        app.page.pop_dialog()
        on_pick(norm_hex(hex_field.value, hex_now()))

    body = ft.Column([
        ft.Row([close, swatch], alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.Container(wheel, alignment=ft.Alignment.CENTER),
        tab_host,
        slider_host,
        ft.Row([hex_field, pill_button("OK", ok, filled=True)], alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
    ], tight=True, spacing=12, width=WHEEL + 16, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    place_marks()
    paint_tabs()
    app.dialog(None, body, [], closable=False)
