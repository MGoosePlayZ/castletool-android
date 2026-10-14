"""The card bar (play / undo / redo / settings, Save) shared by the card screen,
the blueprint screen and the in-app player, plus the card context they share."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import flet as ft

import blueprint_ops as ops
import theme
from session import CardSession
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, text
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

    def edit_sync(self, fn: Callable[[], object]):
        self.session.checkpoint()
        try:
            return fn()
        except Exception:
            self.session.cancel_checkpoint()
            raise
        finally:
            self.session.refresh_dirty()

    async def edit_async(self, fn: Callable[[], object]):
        """Run a blocking edit on a worker thread with an undo checkpoint around it.
        Returns fn's result, or None if it failed (the checkpoint is dropped)."""
        self.session.checkpoint()
        result = await self.app.run(fn)
        if result is None:
            self.session.cancel_checkpoint()
        self.session.refresh_dirty()
        self.app.refresh_bar()
        return result


def card_bar(app, ctx: CardContext, mode: str) -> ft.Control:
    """mode: 'card' | 'blueprint' | 'player'"""
    player = mode == "player"
    s = ctx.session

    # left slot
    if mode == "card":
        left: ft.Control = icon_button(castle_icon("left_arrow", 22), lambda e: app.go_back())
    elif mode == "blueprint":
        left = icon_button(castle_icon("close", 22), lambda e: app.go_back())
    else:
        left = ft.Container()

    def undo(e):
        if s.undo():
            app.refresh()

    def redo(e):
        if s.redo():
            app.refresh()

    def stop(e):
        app.stop_serve()
        app.pop()

    if player:
        main = icon_button(castle_icon("stop", 22), stop, "Stop")
    else:
        main = icon_button(castle_icon("play", 22), lambda e: app.spawn(app.play_card(ctx)), "Play card")
    center = ft.Row([
        main,
        icon_button(castle_icon("undo", 22), undo, "Undo", disabled=player or not s.can_undo),
        icon_button(castle_icon("redo", 22), redo, "Redo", disabled=player or not s.can_redo),
        icon_button(castle_icon("settings", 22), lambda e: card_settings_dialog(app, ctx), "Card settings",
                    disabled=player),
    ], spacing=0, tight=True)

    # right slot
    if player:
        right: ft.Control = ft.Container()
    else:
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
    tempo_box = NumberBox("Tempo", tempo["v"], lambda v: tempo.update(v=v), step=1, minimum=1, maximum=999,
                          decimals=2, width=220)

    def save(e):
        try:
            t = float(tempo["v"])
        except ValueError:
            app.toast("Tempo must be a number.", error=True)
            return
        changed = bg["v"] != card_bg(ctx.card_dir) or abs(t - ops.card_tempo(ctx.card_dir)) > 1e-9
        app.page.pop_dialog()
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

    app.dialog("Card", ft.Column([ft.Row([text("Background", 14, MINOR1, width=90), color.control]),
                                  tempo_box.control], tight=True, spacing=14, width=300), [
        ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=lambda e: app.page.pop_dialog()),
        ft.TextButton(content=text("Save", 14, MAJOR, bold=True), on_click=save)])


async def leave_card(app, ctx: CardContext):
    """Back from the card screen: offer to save unsaved edits."""
    ctx.session.refresh_dirty()
    if not ctx.session.dirty:
        app.pop()
        return

    async def do_save(e):
        app.page.pop_dialog()
        if await app.save_card(ctx):
            app.pop()

    def do_discard(e):
        app.page.pop_dialog()
        ctx.session.discard()
        app.pop()

    app.dialog("Save Changes?", ft.Container(height=0), [
        ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=lambda e: app.page.pop_dialog()),
        ft.TextButton(content=text("Discard", 14, MAJOR), on_click=do_discard),
        ft.TextButton(content=text("Save", 14, MAJOR, bold=True), on_click=do_save)])


def player_screen(app, ctx: CardContext, url: str):
    from app import Screen
    import flet_webview as fwv

    def build(a):
        return ft.Container(
            fwv.WebView(url=url, expand=True, bgcolor=BG,
                        on_web_resource_error=lambda e: a.log(f"webview error: {e.data}")),
            expand=True, alignment=ft.Alignment.TOP_CENTER)

    return Screen(title="Player", build=build, bar=lambda a: card_bar(a, ctx, "player"), on_leave=app.stop_serve)
