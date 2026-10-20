"""Decks (tabs) → Deck (cards) → Card (blueprints) screens."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import flet as ft

import tasks
import theme
from app import App, Screen, profile_screen
from cardbar import CardContext, card_bar, leave_card
from deck_store import card_cover_url, cauldron_status, detect_cauldron, list_local_decks
from theme import BG, MAJOR, MINOR1, SEP, castle_icon, md_icon, minor, text
from widgets import icon_button, pill_button

CARD_RATIO = 7 / 5  # height / width (5:7 cards)
TABS = ["RECENT", "PUBLIC", "UNLISTED", "PRIVATE", "LOCAL", "GLOBAL"]


# ── data helpers ───────────────────────────────────────────────────────────

def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def valid_hex(value, default: str = "#000000") -> str:
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else default


def timestamp(value) -> float:
    """Epoch seconds from an ISO string or epoch milliseconds (0 if unknown)."""
    if value in (None, ""):
        return 0.0
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return float(value) / 1000
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def fmt_date(value) -> str:
    """MM/DD/YY"""
    ts = timestamp(value)
    return datetime.fromtimestamp(ts).strftime("%m/%d/%y") if ts else "--/--/--"


def cover_of(card: dict) -> str:
    """Small cover-image link of a deck.json card entry ('' if it has none)."""
    img = (card or {}).get("backgroundImage")
    return (img.get("smallUrl") or img.get("url") or "") if isinstance(img, dict) else ""


def deck_info(deck_dir: Path) -> dict:
    d = read_json(deck_dir / "deck.json")
    initial = d.get("initialCard") or {}
    return {
        "dir": deck_dir,
        "deckId": d.get("deckId") or deck_dir.name,
        "title": (d.get("title") or "").strip(),
        "caption": " ".join((d.get("caption") or "").split()),
        "visibility": (d.get("visibility") or "").lower(),
        "lastModified": d.get("lastModified"),
        "playCount": d.get("playCount") or 0,
        "bg": valid_hex(initial.get("backgroundColor")),
        "cover": cover_of(initial),
    }


def remote_info(d: dict) -> dict:
    return {
        "dir": None,
        "deckId": d["deckId"],
        "title": (d.get("title") or "").strip() if d.get("title") != "(untitled)" else "",
        "caption": " ".join((d.get("caption") or "").split()),
        "visibility": (d.get("visibility") or "").lower(),
        "lastModified": d.get("lastModified"),
        "playCount": d.get("playCount") or 0,
        "bg": valid_hex(d.get("bg")),
        "cover": d.get("cover") or "",
    }


def tab_decks(app: App, tab: str) -> list[dict]:
    local = {}
    for d in list_local_decks(app.decks_dir):
        info = deck_info(d)
        local[info["deckId"]] = info
    remote = [remote_info(d) for d in app.global_decks if d["deckId"] not in local]   # same ID => the local one wins
    if tab == "LOCAL":
        items = list(local.values())
    elif tab == "GLOBAL":
        items = remote
    else:
        items = list(local.values()) + remote
        if tab in ("PUBLIC", "UNLISTED", "PRIVATE"):
            items = [i for i in items if i["visibility"] == tab.lower()]
    items.sort(key=lambda i: timestamp(i["lastModified"]), reverse=True)
    return items


def card_ids(deck_dir: Path) -> list[str]:
    ids = [c["cardId"] for c in (read_json(deck_dir / "deck.json").get("cards") or []) if c.get("cardId")]
    cards_dir = deck_dir / "cards"
    if cards_dir.is_dir():
        for p in sorted(cards_dir.iterdir()):
            if p.is_dir() and p.name not in ids:
                ids.append(p.name)
    return ids


def card_bg(card_dir: Path) -> str:
    return valid_hex(read_json(card_dir / "card.json").get("backgroundColor"))


def blueprint_files(card_dir: Path) -> list[Path]:
    bp_dir = card_dir / "scene" / "blueprints"
    return sorted(bp_dir.glob("*.json")) if bp_dir.exists() else []


def swatch(bg: str, width: float, cover: str = "") -> ft.Container:
    """A card-shaped thumbnail: the cover image when there is one (the background colour shows
    while it loads, or when it can't, e.g. offline), else just the colour."""
    height = width * CARD_RATIO
    image = ft.Image(src=cover, width=width, height=height, fit=ft.BoxFit.COVER,
                     gapless_playback=True, error_content=ft.Container()) if cover else None
    return ft.Container(image, width=width, height=height, bgcolor=valid_hex(bg),
                        border_radius=width * 0.12, border=ft.Border.all(1, SEP),
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS)


# ── decks ──────────────────────────────────────────────────────────────────

def clone_dialog(app: App):
    field = ft.TextField(hint_text="Deck ID or link", dense=True, border_color=SEP, width=280, autofocus=True,
                         text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    holder: dict = {}

    async def go(e):
        if not field.value:
            return
        path = await app.clone_ref(field.value)      # shows the progress overlay (with its own Cancel)
        if path:
            app.close_dialog(holder["dlg"])
            app.toast("Deck cloned.")

    holder["dlg"] = app.dialog("Clone a deck", ft.Column([
        field, pill_button("Clone", go, filled=True, width=280)], tight=True, spacing=12, width=280),
        [], on_close=app.cancel_all_tasks)


ROW_H = 72          # approximate height of one deck row (for the pull-to-refresh spacer)
PULL_TRIGGER = 80   # overscroll pixels that start a refresh


def home_screen(app: App) -> Screen:
    def left(a: App):
        return icon_button(castle_icon("download2", 22), lambda e: clone_dialog(a), "Clone a deck")

    def actions(a: App):
        return [icon_button(a.avatar(36), lambda e: a.push(profile_screen(a)), "Profile", size=44)]

    def bar_sig(a: App):
        return (id(a.avatar_bytes), a.signed_in)

    def build(a: App) -> ft.Control:
        a.spawn(a.refresh_global())
        st = {"pull": 0.0, "busy": False}
        tab_row = ft.Row(spacing=0)
        list_host = ft.Container(expand=True)
        spinner = ft.Container(ft.ProgressRing(width=20, height=20, stroke_width=2, color=MAJOR), height=36,
                               alignment=ft.Alignment.CENTER, visible=False)

        def paint_tabs():
            tabs = []
            for t in TABS:
                sel = t == a.decks_tab

                def pick(e, tt=t):
                    a.decks_tab = tt            # only the tab strip and the list change; the top bar stays
                    paint_tabs()
                    fill_list()
                    a.page.update()
                tabs.append(ft.Container(
                    text(t, 11, MAJOR if sel else MINOR1, bold=sel, no_wrap=True), expand=True,
                    alignment=ft.Alignment.CENTER, padding=ft.Padding.symmetric(vertical=10, horizontal=1),
                    ink=True, on_click=pick,
                    border=ft.Border(bottom=ft.BorderSide(2 if sel else 1, MAJOR if sel else SEP))))
            tab_row.controls = tabs

        async def do_refresh():
            if st["busy"]:
                return
            st["busy"] = True
            spinner.visible = True
            a.page.update()
            if a.signed_in:
                await a.refresh_profile()
                if not await a.refresh_global(force=True, repaint=False) and not a.online:
                    a.toast("No Wi-Fi.", error=True)
            fill_list()
            spinner.visible = False
            st["busy"] = False
            a.page.update()

        def on_scroll(e):
            kind = getattr(e.event_type, "value", e.event_type)
            if kind == "overscroll" and (e.overscroll or 0) < 0:       # dragging down past the top
                st["pull"] += -e.overscroll
                if st["pull"] >= PULL_TRIGGER and not st["busy"]:
                    st["pull"] = 0.0
                    a.spawn(do_refresh())
            elif kind in ("start", "end"):
                st["pull"] = 0.0

        def fill_list():
            items = tab_decks(a, a.decks_tab)
            if not items:
                list_host.content = ft.ListView(
                    [ft.Container(minor("No decks" if a.signed_in else "You are not signed in", 13),
                                   alignment=ft.Alignment.CENTER, height=200),
                     ft.Container(height=(a.page.height or 800))],
                    expand=True, on_scroll=on_scroll, scroll_interval=10)
                return
            # a spacer keeps the list scrollable even when it is short, so pulling down always works
            spacer = max(0, (a.page.height or 800) - len(items) * ROW_H - 120)
            list_host.content = ft.ListView([deck_row(a, d) for d in items] + [ft.Container(height=spacer + 2)],
                                            spacing=0, expand=True, on_scroll=on_scroll, scroll_interval=10)

        paint_tabs()
        fill_list()
        return ft.Column([tab_row, spinner, list_host], spacing=0, expand=True)

    return Screen(title="Decks", build=build, left=left, actions=actions, sig=bar_sig)


def deck_row(app: App, d: dict) -> ft.Control:
    label = d["title"] or d["caption"] or d["deckId"]       # no title → the description
    date = ft.Row([md_icon("calendar_today", 14, MINOR1), minor(fmt_date(d["lastModified"]), 12)], spacing=4, tight=True)
    plays = ft.Row([castle_icon("play", 14, MINOR1), minor(f"{d['playCount']:,}", 12)], spacing=4, tight=True)
    local_dir: Optional[Path] = d["dir"]
    return ft.Container(
        content=ft.Row([
            swatch(d["bg"], 36, d.get("cover", "")),
            ft.Container(text(label, 15, medium=True, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS), expand=True),
            ft.Container(date, width=82),
            ft.Container(plays, width=52, alignment=ft.Alignment.CENTER_RIGHT),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
        padding=ft.Padding.symmetric(vertical=10, horizontal=14),
        border=ft.Border(bottom=ft.BorderSide(1, SEP)), ink=True,
        on_click=lambda e: app.spawn(app.open_deck(d["deckId"], local_dir)),
        on_long_press=(lambda e: app.ask_delete_deck(local_dir, label)) if local_dir else None,
    )


# ── deck: grid of cards ────────────────────────────────────────────────────

def deck_screen(app: App, deck_dir: Path) -> Screen:
    state = {"alive": True}

    def cards_view(a: App) -> ft.Control:
        tiles = []
        for cid in card_ids(deck_dir):
            cdir = deck_dir / "cards" / cid
            tiles.append(ft.Container(
                content=ft.Column([
                    swatch(card_bg(cdir), 104, card_cover_url(deck_dir, cid)),
                    minor(cid, 10, text_align=ft.TextAlign.CENTER, width=104, max_lines=2),
                ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                ink=True, border_radius=12,
                on_click=lambda e, c=cid: a.push(card_screen(a, deck_dir, c)),
            ))
        if not tiles:
            return ft.Container(minor("No cards", 13), alignment=ft.Alignment.CENTER, expand=True)
        return ft.Column([ft.Container(ft.Row(tiles, wrap=True, spacing=12, run_spacing=14,
                                              alignment=ft.MainAxisAlignment.CENTER), padding=14)],
                         scroll=ft.ScrollMode.AUTO, expand=True)

    def body(a: App, cauldron: bool) -> ft.Control:
        if cauldron:        # nothing to do with the cards of a cauldron deck, so they aren't shown
            return ft.Container(minor("This deck uses cauldron", 13), alignment=ft.Alignment.CENTER, expand=True)
        return cards_view(a)

    def build(a: App) -> ft.Control:
        state["alive"] = True
        known, needs_fetch = cauldron_status(a.cauldron_cache, deck_dir)
        if known is not None or not needs_fetch:
            return body(a, bool(known))
        host = ft.Container(ft.ProgressRing(width=20, height=20, stroke_width=2, color=MAJOR),
                            alignment=ft.Alignment.CENTER, expand=True)

        async def check():
            try:
                await asyncio.to_thread(detect_cauldron, a.api, deck_dir, a.cauldron_cache)
            except Exception as ex:             # offline etc.: show the cards, ask again next time
                a.log(f"cauldron check: {ex}", 2)
                if state["alive"]:
                    host.alignment = None
                    host.content = body(a, False)
                    a.page.update()
                return
            if state["alive"]:
                # The answer is cached now, so rebuilding takes the same path as re-entering the
                # screen. Swapping the cards into the spinner's centred placeholder left them
                # offset until the screen was rebuilt ("ghost" card).
                a.refresh_body_if("Deck")

        a.page.run_task(check)
        return host

    return Screen(title="Deck", build=build, on_leave=lambda: state.update(alive=False))


# ── card: list of blueprints ───────────────────────────────────────────────

def card_screen(app: App, deck_dir: Path, card_id: str) -> Screen:
    ctx = CardContext(app, deck_dir, card_id)
    card_dir = ctx.card_dir
    thumb = {"task": None}

    def stop_thumbs():
        if thumb["task"]:
            thumb["task"].cancel()

    def build(a: App) -> ft.Control:
        from screens_blueprint import blueprint_screen
        stop_thumbs()
        files = blueprint_files(card_dir)
        if not files:
            return ft.Container(minor("No blueprints", 13), alignment=ft.Alignment.CENTER, expand=True)
        a.icons.prune(card_dir, {f.stem for f in files})
        entries = []
        for f in files:
            try:
                entries.append((a.icons.meta(card_dir, f), f))      # reads the blueprint only when it changed
            except Exception as ex:
                a.log(f"error: {f.name}: {ex}")
        entries.sort(key=lambda t: t[0]["title"].lower())
        rows, pending = [], []
        for e, f in entries:
            holder = ft.Container(width=52, height=52, alignment=ft.Alignment.CENTER)   # no card behind the icon
            if e["has_drawing"]:
                png = a.icons.png(card_dir, f)
                if png:
                    holder.content = ft.Image(src=png, width=48, height=48, fit=ft.BoxFit.CONTAIN,
                                              gapless_playback=True)
                else:
                    pending.append((holder, f))
            else:
                holder.content = castle_icon("brick", 22, MINOR1)
            rows.append(ft.Container(
                content=ft.Row([holder, text(e["title"], 15, medium=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                             expand=True)],
                               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                padding=ft.Padding.symmetric(vertical=8, horizontal=14),
                border=ft.Border(bottom=ft.BorderSide(1, SEP)), ink=True,
                on_click=lambda ev, ff=f: a.push(blueprint_screen(a, ctx, ff)),
            ))

        if pending:
            task = tasks.Task("icons")
            thumb["task"] = task

            def make(f: Path):
                try:
                    with tasks.use(task):
                        return a.icons.render(card_dir, f)
                except tasks.Cancelled:
                    return None

            async def load_thumbs():
                import asyncio
                for holder, f in pending:
                    if task.cancelled:
                        return
                    try:
                        png = await asyncio.to_thread(make, f)
                    except Exception:              # unreadable: leave the placeholder
                        continue
                    if png and not task.cancelled:
                        holder.content = ft.Image(src=png, width=48, height=48, fit=ft.BoxFit.CONTAIN,
                                                  gapless_playback=True)
                        a.page.update()

            a.page.run_task(load_thumbs)
        return ft.ListView(rows, spacing=0, expand=True)

    return Screen(title="Card", build=build, bar=lambda a: card_bar(a, ctx, "card"),
                  on_back=lambda: leave_card(app, ctx), on_leave=stop_thumbs, sig=lambda a: ctx.bar_sig())
