"""Reusable controls: number box (digits only, wheel + +/- buttons), color field
(swatch + hex, tap for a picker), and the key-info button for real explanations."""
from __future__ import annotations

import colorsys
import re
from typing import Callable, Optional

import flet as ft

import theme
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, text

FIELD_TEXT = ft.TextStyle(font_family=theme.BODY, color=MAJOR, size=14)


def icon_button(icon: ft.Control, on_click=None, tooltip: Optional[str] = None, size: int = 40,
                disabled: bool = False) -> ft.Container:
    return ft.Container(
        content=icon, width=size, height=size, alignment=ft.Alignment.CENTER,
        border_radius=size / 2, ink=not disabled, on_click=None if disabled else on_click,
        tooltip=tooltip, opacity=0.3 if disabled else 1.0,
    )


def pill_button(label: str, on_click, icon: Optional[ft.Control] = None, disabled: bool = False,
                filled: bool = False, busy: bool = False, width: Optional[float] = None) -> ft.Container:
    """Monochrome button. Filled = white with black text. Disabled filled = separator gray.
    `busy` shows a throbber in place of the label."""
    if filled:
        fg = BG
        bg = SEP if (disabled and not busy) else MAJOR
    else:
        fg, bg = MAJOR, None
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

def info_button(app, message: str, heading: str = "") -> ft.Control:
    """key-info.png at 25% opacity; tapping explains something that really needs it."""
    img = ft.Image(src="icons/key-info.png", width=18, height=18, opacity=0.25, fit=ft.BoxFit.CONTAIN,
                   error_content=ft.Text("?", size=14, color=MINOR1))

    def show(e):
        app.dialog(heading or "Info", text(message, 14, MINOR1), [
            ft.TextButton(content=text("OK", 14, MAJOR, bold=True), on_click=lambda e: app.page.pop_dialog())])
    return ft.Container(img, width=28, height=28, alignment=ft.Alignment.CENTER, border_radius=14, ink=True,
                        on_click=show)


# ── number box ─────────────────────────────────────────────────────────────

class NumberBox:
    """Digits-only TextField with +/- buttons and mouse-wheel stepping.
    `.control` is the thing to add to a layout; `.value` is the current text."""

    def __init__(self, label: str, value, on_change: Callable[[str], None], step: float = 1,
                 minimum: Optional[float] = None, maximum: Optional[float] = None,
                 decimals: int = 0, width: int = 170, enabled: bool = True):
        self.step, self.min, self.max, self.decimals = step, minimum, maximum, decimals
        self.on_change = on_change
        regex = r"^[0-9]*\.?[0-9]*$" if decimals else r"^[0-9]*$"
        self.field = ft.TextField(
            value=self._fmt(value), label=label, dense=True, border_color=SEP, text_style=FIELD_TEXT,
            keyboard_type=ft.KeyboardType.NUMBER, input_filter=ft.InputFilter(regex_string=regex, allow=True),
            on_change=self._typed, expand=True, disabled=not enabled,
            label_style=ft.TextStyle(font_family=theme.BODY, color=MINOR1, size=12))
        minus = icon_button(castle_icon("horizontal_line", 18), lambda e: self._bump(-1), size=34, disabled=not enabled)
        plus = icon_button(castle_icon("add", 18), lambda e: self._bump(1), size=34, disabled=not enabled)
        self.control = ft.GestureDetector(
            content=ft.Row([self.field, minus, plus], spacing=0, width=width,
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


def open_color_picker(app, current: str, on_pick: Callable[[str], None]):
    r, g, b = (int(norm_hex(current)[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    state = {"h": h * 360, "s": s * 100, "v": v * 100}

    def to_hex() -> str:
        rr, gg, bb = colorsys.hsv_to_rgb(state["h"] / 360, state["s"] / 100, state["v"] / 100)
        return "#%02x%02x%02x" % (round(rr * 255), round(gg * 255), round(bb * 255))

    preview = ft.Container(height=56, border_radius=10, bgcolor=to_hex(), border=ft.Border.all(1, SEP))
    hex_field = ft.TextField(value=to_hex(), dense=True, border_color=SEP, text_style=FIELD_TEXT, width=140,
                             input_filter=ft.InputFilter(regex_string=r"^#?[0-9a-fA-F]{0,6}$", allow=True))
    sliders: dict[str, ft.Slider] = {}

    def sync_from_state():
        c = to_hex()
        preview.bgcolor = c
        hex_field.value = c
        app.page.update()

    def make(key: str, label: str, top: int):
        def ch(e, k=key):
            state[k] = e.control.value
            sync_from_state()
        sl = ft.Slider(min=0, max=top, value=state[key], on_change=ch, active_color=MAJOR, inactive_color=SEP,
                       expand=True)
        sliders[key] = sl
        return ft.Row([text(label, 13, MINOR1, width=14), sl], spacing=6)

    def typed(e):
        c = norm_hex(hex_field.value, "")
        if c:
            rr, gg, bb = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
            hh, ss, vv = colorsys.rgb_to_hsv(rr, gg, bb)
            state.update(h=hh * 360, s=ss * 100, v=vv * 100)
            for k in ("h", "s", "v"):
                sliders[k].value = state[k]
            preview.bgcolor = c
            app.page.update()

    hex_field.on_change = typed
    body = ft.Column([preview, make("h", "H", 360), make("s", "S", 100), make("v", "V", 100), hex_field],
                     tight=True, spacing=8, width=280)

    def ok(e):
        app.page.pop_dialog()
        on_pick(norm_hex(hex_field.value, to_hex()))

    app.dialog("Color", body, [
        ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=lambda e: app.page.pop_dialog()),
        ft.TextButton(content=text("OK", 14, MAJOR, bold=True), on_click=ok)])
