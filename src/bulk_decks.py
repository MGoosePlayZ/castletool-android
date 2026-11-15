"""Bulk deck actions on the Decks screen.

Long-press a deck to start selecting; the top bar turns into a selection bar:
  left  ci-close  deselects everything
  right ci-more   → Set Visibility… / Delete Selected
"""
from __future__ import annotations

import asyncio
import shutil
from typing import Callable

import flet as ft

import theme
from app import App, _read_json, _write_json
from deck_store import _read_json as _read_deck_json, _write_json as _write_deck_json
from session import deck_has_unsaved
from theme import BG, DANGER, MAJOR, MINOR1, SEP, castle_icon, text
from widgets import icon_button

VISIBILITIES = [("public", "Public"), ("unlisted", "Unlisted"), ("private", "Private")]
PAUSE = 0.12            # seconds between requests (be nice to Castle)


# ── selection ──────────────────────────────────────────────────────────────

def toggle(a: App, deck_id: str, name: str):
    if deck_id in a.deck_sel:
        del a.deck_sel[deck_id]
    else:
        a.deck_sel[deck_id] = name
    a.deck_sel_changed()


def clear(a: App):
    a.deck_sel.clear()
    a.deck_sel_changed()


def selection_bar(a: App) -> ft.Control | None:
    """The top bar while decks are selected (None otherwise → the normal bar)."""
    if not a.deck_sel:
        return None
    more = ft.PopupMenuButton(
        content=ft.Container(castle_icon("more", 22), width=44, height=44, alignment=ft.Alignment.CENTER),
        bgcolor=BG, shape=theme.popup_shape(10), tooltip="More",
        items=[
            ft.PopupMenuItem(content=ft.Row([castle_icon("people", 20), text("Set Visibility...", 14)], spacing=10),
                             on_click=lambda e: visibility_dialog(a)),
            ft.PopupMenuItem(content=ft.Row([castle_icon("delete", 20, DANGER), text("Delete Selected", 14, DANGER)],
                                            spacing=10),
                             on_click=lambda e: delete_selected(a)),
        ])
    return ft.Container(ft.Row([
        ft.Container(icon_button(castle_icon("close", 22), lambda e: clear(a), "Deselect all", size=44),
                     width=56, alignment=ft.Alignment.CENTER_LEFT),
        ft.Container(expand=True),
        ft.Container(more, width=56, alignment=ft.Alignment.CENTER_RIGHT),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=0),
        padding=ft.Padding(left=6, right=6, top=4, bottom=4))


# ── running one request per deck ───────────────────────────────────────────

async def run_bulk(a: App, heading: str, ids: list[str], work: Callable) -> list[tuple[str, str]]:
    """`work(deck_id)` is an async fn. Shows a progress modal; returns [(deckId, error)] for the failures."""
    status = text(f"0 / {len(ids)}", 14, MINOR1)
    dlg = a.dialog(heading, ft.Row([ft.ProgressRing(width=20, height=20, stroke_width=2, color=MAJOR), status],
                                   spacing=12), [], closable=False)
    fails: list[tuple[str, str]] = []
    for i, did in enumerate(ids):
        status.value = f"{i + 1} / {len(ids)}"
        a.page.update()
        try:
            await work(did)
        except Exception as ex:
            fails.append((did, str(ex) or ex.__class__.__name__))
            a.log(f"error: {heading}: {did}: {ex}")
        await asyncio.sleep(PAUSE)
    a.close_dialog(dlg)
    return fails


def report(a: App, ok_msg: str, ids: list[str], fails: list[tuple[str, str]]):
    names = dict(a.deck_sel)
    a.deck_sel.clear()
    a.deck_sel_changed()
    a.refresh_body_if("Decks")
    if not fails:
        a.toast(ok_msg)
        return
    lines = "\n".join(f"{names.get(d) or d}: {m}" for d, m in fails[:8])
    more = f"\n…and {len(fails) - 8} more" if len(fails) > 8 else ""
    a.info(f"{len(ids) - len(fails)} done, {len(fails)} failed", lines + more)


# ── visibility ─────────────────────────────────────────────────────────────

def visibility_dialog(a: App):
    ids = list(a.deck_sel)
    if not ids:
        return
    pick = ft.Dropdown(value="private", dense=True, border_color=SEP, width=240,
                       options=[ft.dropdown.Option(key=v, text=t) for v, t in VISIBILITIES],
                       text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    holder: dict = {}

    async def go(e):
        a.close_dialog(holder["dlg"])
        if not await a.ensure_online():
            return
        await set_visibility(a, ids, pick.value or "private")

    holder["dlg"] = a.dialog(
        "Set Visibility",
        ft.Column([text(f"{len(ids)} deck{'s' if len(ids) != 1 else ''} selected.", 14, MINOR1), pick],
                  tight=True, spacing=12),
        [ft.TextButton(content=text("Cancel", 14, MINOR1), on_click=lambda e: a.close_dialog(holder["dlg"])),
         ft.TextButton(content=text("Confirm", 14, MAJOR, bold=True), on_click=go)],
        closable=False)


async def set_visibility(a: App, ids: list[str], visibility: str):
    info = {d["deckId"]: d for d in a.global_decks}

    async def work(did: str):
        d = info.get(did) or {}
        local = a.decks_dir / did
        title, caption = d.get("title") or "", d.get("caption") or ""
        if local.is_dir():
            ld = _read_deck_json(local / "deck.json")
            title, caption = ld.get("title") or title, ld.get("caption") or caption
        title = "" if title == "(untitled)" else title
        got = await asyncio.to_thread(a.api.set_deck_visibility, did, visibility, title, caption)
        for g in a.global_decks:
            if g.get("deckId") == did:
                g["visibility"] = got
        if local.is_dir():                                  # keep the copy on this device in step, or the
            ld = _read_deck_json(local / "deck.json")       # next save would put the old visibility back
            ld["visibility"] = got
            _write_deck_json(local / "deck.json", ld)

    fails = await run_bulk(a, "Setting visibility", ids, work)
    _write_json(a.global_file, a.global_decks)
    report(a, f"Visibility set to {visibility}.", ids, fails)


# ── delete ─────────────────────────────────────────────────────────────────

def delete_selected(a: App):
    ids = list(a.deck_sel)
    if not ids:
        return
    unsaved = any(deck_has_unsaved(a.decks_dir / d) for d in ids if (a.decks_dir / d).is_dir())
    tail = " Unsaved changes on this device are lost too." if unsaved else ""
    if len(ids) == 1:
        a.confirm(f"Delete \"{a.deck_sel[ids[0]]}\"?",
                  "This permanently deletes the deck from Castle and from this device." + tail, "Delete",
                  lambda: delete_decks(a, ids))
        return
    holder: dict = {}

    def accept(e):
        a.close_dialog(holder["dlg"])
        username_step(a, ids)

    holder["dlg"] = a.dialog(
        None,
        text("You are about to permanently delete multiple decks at once. This is dangerous!" + tail, 15, DANGER),
        [ft.TextButton(content=text("Go Back", 14, MAJOR), on_click=lambda e: a.close_dialog(holder["dlg"])),
         ft.TextButton(content=text("I understand and accept these risks", 14, DANGER, bold=True), on_click=accept)],
        closable=False)


def username_step(a: App, ids: list[str]):
    username = ((a.user or {}).get("username") or "").strip()
    if not username:
        a.toast("Sign in first.", error=True)
        return
    holder: dict = {}
    n = len(ids)
    field = ft.TextField(hint_text=username, label="Type your username", dense=True, border_color=SEP, width=260,
                         autofocus=True, text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    label = text(f"Delete {n} Decks", 14, MINOR1, bold=True)
    go = ft.TextButton(content=label, disabled=True)

    def check(e):
        ok = (field.value or "").strip().casefold() == username.casefold()
        go.disabled = not ok
        label.color = DANGER if ok else MINOR1
        a.page.update()

    async def run(e):
        if go.disabled:
            return
        a.close_dialog(holder["dlg"])
        await delete_decks(a, ids)

    field.on_change = check
    go.on_click = run
    holder["dlg"] = a.dialog(
        None,
        ft.Column([text(f"To delete {n} decks, type your username ({username}).", 14, MINOR1), field],
                  tight=True, spacing=12),
        [ft.TextButton(content=text("Go Back", 14, MAJOR), on_click=lambda e: a.close_dialog(holder["dlg"])), go],
        closable=False)


async def delete_decks(a: App, ids: list[str]):
    if not await a.ensure_online():
        return

    async def work(did: str):
        await asyncio.to_thread(a.api.delete_deck, did)
        a.global_decks = [g for g in a.global_decks if g.get("deckId") != did]
        local = a.decks_dir / did
        if local.is_dir():
            await asyncio.to_thread(shutil.rmtree, local, True)
            await asyncio.to_thread(a.icons.delete_deck, local)
        a.clear_sessions(did)

    fails = await run_bulk(a, "Deleting", ids, work)
    _write_json(a.global_file, a.global_decks)
    n = len(ids) - len(fails)
    report(a, f"Deleted {n} deck{'s' if n != 1 else ''}.", ids, fails)
