"""The card bar (undo / redo / settings, Save) shared by the card screen and the blueprint
screen, plus the card context they share."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import flet as ft

import blueprint_ops as ops
import theme
from session import CardSession
from theme import BG, DANGER, MAJOR, MINOR1, SEP, castle_icon, text
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

    def bar_sig(self):
        s = self.session
        return (s.can_undo, s.can_redo, s.dirty, self.saving)

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
    from screens_library import card_bg
    bg = {"v": card_bg(ctx.card_dir)}
    tempo = {"v": f"{ops.card_tempo(ctx.card_dir):g}"}
    color = ColorField(app, bg["v"], lambda v: bg.update(v=v))
    tempo_box = NumberBox(None, tempo["v"], lambda v: tempo.update(v=v), step=1, minimum=1, maximum=999,
                          decimals=2, width=200)
    holder: dict = {}

    def save(e):
        try:
            t = float(tempo["v"])
        except ValueError:
            app.toast("Tempo must be a number.", error=True)
            return
        changed = bg["v"] != card_bg(ctx.card_dir) or abs(t - ops.card_tempo(ctx.card_dir)) > 1e-9
        app.close_dialog(holder["dlg"])
        if not changed:
            return
        try:
            def apply():
                ops.set_card_background(ctx.card_dir, bg["v"])
                ops.set_card_tempo(ctx.card_dir, t)
            ctx.edit_sync(apply)
        except ops.OpError as ex:
            app.toast(str(ex), error=True)
            return
        app.refresh()

    row = lambda icon, control: ft.Row([ft.Container(castle_icon(icon, 22, MINOR1), width=34), control],
                                       vertical_alignment=ft.CrossAxisAlignment.CENTER)
    holder["dlg"] = app.dialog("Card", ft.Column([row("bucket", color.control), row("drums", tempo_box.control)],
                                                 tight=True, spacing=14, width=280), [
        ft.TextButton(content=text("Save", 14, MAJOR, bold=True), on_click=save)])


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
