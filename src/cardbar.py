"""The card bar (undo / redo / settings, Save) shared by the card screen and the blueprint
screen, plus the card context they share."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

import flet as ft

import blueprint_ops as ops
import deck_store
import tasks
import theme
from session import CardSession
from castle_api import UPLOAD_LIMIT_BYTES
from theme import BG, DANGER, MAJOR, MINOR1, SEP, WARN, castle_icon, text
from widgets import ColorField, NumberBox, icon_button, pill_button

SIDE = 88   # width of the left/right slots, so the centre cluster is truly centred


class CardContext:
    """One open card: where it lives, its undo/dirty session, and save state."""

    def __init__(self, app, deck_dir: Path, card_id: str):
        self.app = app
        self.deck_dir = Path(deck_dir)
        self.card_id = card_id
        self.card_dir = self.deck_dir / "cards" / card_id
        self.session: CardSession = app.session_for(self.deck_dir, card_id)
        self.saving = False

    def size_bytes(self) -> int:
        """Bytes of this card's scene data as Castle receives it (what its 10 MB limit measures).
        Rebuilt only when a file in the card's folder changed."""
        stamp = []
        for root, _dirs, files in os.walk(self.card_dir):
            for name in files:
                try:
                    st = os.stat(os.path.join(root, name))
                except OSError:
                    continue
                stamp.append((root, name, st.st_mtime_ns, st.st_size))
        stamp = tuple(sorted(stamp))
        if getattr(self, "_size_stamp", None) != stamp:
            try:
                self._size_bytes = deck_store.card_upload_size(self.card_dir)
            except (OSError, ValueError):
                self._size_bytes = 0
            self._size_stamp = stamp
        return self._size_bytes

    def bar_sig(self):
        s = self.session
        return (s.can_undo, s.can_redo, s.dirty, self.saving, self.size_bytes())

    def edit_sync(self, fn: Callable[[], object]):
        self.session.checkpoint()
        try:
            return fn()
        except Exception:
            self.session.rollback()
            raise
        finally:
            self.session.refresh_dirty()

    async def edit_async(self, label: str, fn: Callable):
        """Run a blocking edit `fn(task)` on a worker thread, with progress, cancel and an undo
        checkpoint around it. Returns fn's result, or None if it failed or was cancelled
        (then the card is put back exactly as it was)."""
        self.session.checkpoint()
        result = await self.app.work(label, fn)
        if result is None:
            self.session.rollback()
        self.session.refresh_dirty()
        self.app.refresh_bar()
        return result


def size_meter(ctx: CardContext) -> ft.Control:
    """How full the card is against Castle's 10 MB limit: white under 50%, yellow over 50%, red over 75%."""
    size = ctx.size_bytes()
    frac = size / UPLOAD_LIMIT_BYTES
    pct = round(frac * 100)
    color = DANGER if frac > 0.75 else WARN if frac > 0.5 else MAJOR
    label = f"{size / 1048576:.2f} MB of {UPLOAD_LIMIT_BYTES // 1048576} MB"
    ring = ft.ProgressRing(value=min(1.0, frac), width=32, height=32, stroke_width=3, color=color, bgcolor=SEP)
    num = ft.Container(text(f"{min(pct, 999)}%", 8 if pct > 99 else 9, color, bold=True), width=32, height=32,
                       alignment=ft.Alignment.CENTER)
    return ft.Container(ft.Stack([ring, num], width=32, height=32), width=36, height=40,
                        alignment=ft.Alignment.CENTER, tooltip=label)


def card_bar(app, ctx: CardContext, mode: str) -> ft.Control:
    """mode: 'card' | 'blueprint'"""
    s = ctx.session

    if mode == "card":
        left: ft.Control = icon_button(castle_icon("left_arrow", 22), lambda e: app.go_back())
    else:
        left = icon_button(castle_icon("close", 22), lambda e: app.go_back())

    def undo(e):
        if s.undo():
            app.refresh()

    def redo(e):
        if s.redo():
            app.refresh()

    center = ft.Row([
        size_meter(ctx),
        icon_button(castle_icon("undo", 22), undo, "Undo", disabled=not s.can_undo),
        icon_button(castle_icon("redo", 22), redo, "Redo", disabled=not s.can_redo),
        icon_button(castle_icon("settings", 22), lambda e: card_settings_dialog(app, ctx), "Card settings"),
    ], spacing=0, tight=True)

    right = pill_button("Save", lambda e: app.spawn(app.save_card(ctx)), filled=True,
                        disabled=not s.dirty, busy=ctx.saving)

    return ft.Container(ft.Row([
        ft.Container(left, width=SIDE, alignment=ft.Alignment.CENTER_LEFT),
        ft.Container(center, expand=True, alignment=ft.Alignment.CENTER),
        ft.Container(right, width=SIDE, alignment=ft.Alignment.CENTER_RIGHT),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=0),
        padding=ft.Padding(left=6, right=6, top=4, bottom=4))


def card_settings_dialog(app, ctx: CardContext):
    from screens_library import card_bg, swatch
    bg = {"v": card_bg(ctx.card_dir)}
    tempo = {"v": f"{ops.card_tempo(ctx.card_dir):g}"}
    holder: dict = {}

    def apply(quiet: bool = False) -> bool:
        """Write the dialog's values into the card buffer (not to Castle). Does nothing when
        nothing differs, so it is safe to call from every route that closes the dialog."""
        try:
            t = float(tempo["v"])
        except ValueError:
            if not quiet:
                app.toast("Tempo must be a number.", error=True)
            t = ops.card_tempo(ctx.card_dir)             # bad tempo text: keep the old tempo
        if bg["v"] == card_bg(ctx.card_dir) and abs(t - ops.card_tempo(ctx.card_dir)) <= 1e-9:
            return False
        try:
            def write():
                ops.set_card_background(ctx.card_dir, bg["v"])
                ops.set_card_tempo(ctx.card_dir, t)
            ctx.edit_sync(write)
        except ops.OpError as ex:
            app.toast(str(ex), error=True)
            return False
        app.refresh()
        return True

    def picked(v: str):                                  # a color change lands in the buffer at once
        bg["v"] = v
        apply()

    color = ColorField(app, bg["v"], picked)
    # The tempo is typed digit by digit, so it lands in the buffer when the dialog closes
    # (OK, the X, or tapping outside), not on every keystroke.
    tempo_box = NumberBox(None, tempo["v"], lambda v: tempo.update(v=v), step=1, minimum=1, maximum=999,
                          decimals=2, width=200)

    def ok(e):
        app.close_dialog(holder["dlg"])
        apply()

    async def pick_cover(e):
        path = await app.pick(["png", "jpg", "jpeg"], "Choose a cover image")
        if not path:
            return
        if not await app.ensure_online():
            return
        app.close_dialog(holder["dlg"])

        def upload(task):
            with tasks.use(task):
                tasks.say("Uploading image", 0.2)
                result = app.api.upload_file(path)
                deck_store.set_pending_cover(ctx.card_dir, result["fileId"], result["url"])
            return True

        if await ctx.edit_async("Uploading cover", upload):       # undo / discard put the old cover back
            app.toast("Cover changed. Save to apply it.")
            app.refresh()

    cover_btn = ft.Container(ft.Row([
        swatch(bg["v"], 28, deck_store.card_cover_url(ctx.deck_dir, ctx.card_id)),
        text("Cover image", 14, MAJOR, bold=True)], spacing=10, tight=True),
        ink=True, on_click=pick_cover, border_radius=8, padding=ft.Padding.symmetric(vertical=4, horizontal=6))
    row = lambda icon, control: ft.Row([ft.Container(castle_icon(icon, 22, MINOR1), width=34), control],
                                       vertical_alignment=ft.CrossAxisAlignment.CENTER)
    holder["dlg"] = app.dialog("Card", ft.Column([row("bucket", color.control), row("drums", tempo_box.control),
                                                  ft.Row([ft.Container(castle_icon("picture", 22, MINOR1), width=34), cover_btn],
                                                         vertical_alignment=ft.CrossAxisAlignment.CENTER)],
                                                 tight=True, spacing=14, width=280), [
        ft.TextButton(content=text("OK", 14, MAJOR, bold=True), on_click=ok)],
        on_close=lambda: apply(quiet=True))


async def leave_card(app, ctx: CardContext):
    """Back from the card screen: offer to save unsaved edits."""
    ctx.session.refresh_dirty()
    if not ctx.session.dirty:
        app.pop()
        return
    holder: dict = {}

    async def do_save(e):
        app.close_dialog(holder["dlg"])
        if await app.save_card(ctx):
            app.pop()

    def do_discard(e):
        app.close_dialog(holder["dlg"])
        ctx.session.discard()
        app.pop()

    body = ft.Column([
        pill_button("Save", do_save, filled=True, width=260),
        pill_button("Discard", do_discard, width=260, color=DANGER),
    ], spacing=10, tight=True, horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    holder["dlg"] = app.dialog("Save Changes?", ft.Container(body, width=260, padding=ft.Padding(top=8)), [])
