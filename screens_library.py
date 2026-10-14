"""Decks (tabs) → Deck (cards) → Card (blueprints) screens."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import flet as ft

import blueprint_ops as ops
import drawing
import theme
from app import App, Screen, profile_screen
from cardbar import CardContext, card_bar, leave_card
from deck_store import list_local_decks
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


def swatch(bg: str, width: float) -> ft.Container:
    return ft.Container(width=width, height=width * CARD_RATIO, bgcolor=valid_hex(bg),
                        border_radius=width * 0.12, border=ft.Border.all(1, SEP))


# ── thumbnails (rendered from the blueprint JSON) ──────────────────────────
_thumb_cache: dict = {}


def blueprint_thumb(bp_path: Path, bg: str, size: int = 96) -> Optional[bytes]:
    st = bp_path.stat()
    key = (str(bp_path), st.st_mtime_ns, st.st_size, bg, size)
    if key not in _thumb_cache:
        _thumb_cache[key] = drawing.render_png(ops.load_blueprint(bp_path), size, bg)
        if len(_thumb_cache) > 400:
            _thumb_cache.pop(next(iter(_thumb_cache)))
    return _thumb_cache[key]


def _safe_thumb(f: Path, bg: str):
    try:
        return blueprint_thumb(f, bg, 96)
    except Exception:
        return None


# ── decks ──────────────────────────────────────────────────────────────────

def clone_dialog(app: App):
    field = ft.TextField(hint_text="Deck ID or link", dense=True, border_color=SEP, width=280, autofocus=True,
                         text_style=ft.TextStyle(font_family=theme.BODY, color=MAJOR))
    host = ft.Container()
    state = {"busy": False}

    def paint():
        host.content = pill_button("Clone", go, filled=True, busy=state["busy"], width=280)
        app.page.update()

    async def go(e):
        if not field.value or state["busy"]:
            return
        state["busy"] = True
        paint()
        path = await app.clone_ref(field.value)
        state["busy"] = False
        if path:
            app.page.pop_dialog()
            app.toast("Deck cloned.")
        else:
            paint()

    paint()
    app.dialog("Clone a deck", ft.Column([field, host], tight=True, spacing=12, width=280), [
        ft.TextButton(content=text("Close", 14, MINOR1), on_click=lambda e: app.page.pop_dialog())])


def home_screen(app: App) -> Screen:
    def left(a: App):
        return icon_button(castle_icon("download2", 22), lambda e: clone_dialog(a), "Clone a deck")

    def actions(a: App):
        return [icon_button(a.avatar(36), lambda e: a.push(profile_screen(a)), "Profile", size=44)]

    def build(a: App) -> ft.Control:
        a.spawn(a.refresh_global())
        tabs = []
        for t in TABS:
            sel = t == a.decks_tab

            def pick(e, tt=t):
                a.decks_tab = tt
                a.refresh()
            tabs.append(ft.Container(
                text(t, 13, MAJOR if sel else MINOR1, bold=sel), padding=ft.Padding.symmetric(vertical=10, horizontal=12),
                ink=True, on_click=pick))
        items = tab_decks(a, a.decks_tab)
        if items:
            body: ft.Control = ft.ListView([deck_row(a, d) for d in items], spacing=0, expand=True)
        else:
            body = ft.Container(minor("No decks", 13), alignment=ft.Alignment.CENTER, expand=True)
        return ft.Column([
            ft.Container(ft.Row(tabs, scroll=ft.ScrollMode.AUTO, spacing=0), border=ft.Border(bottom=ft.BorderSide(1, SEP))),
            body], spacing=0, expand=True)

    return Screen(title="Decks", build=build, left=left, actions=actions)


def deck_row(app: App, d: dict) -> ft.Control:
    label = d["title"] or d["caption"] or d["deckId"]       # no title → the description
    date = ft.Row([md_icon("calendar_today", 14, MINOR1), minor(fmt_date(d["lastModified"]), 12)], spacing=4, tight=True)
    plays = ft.Row([castle_icon("play", 14, MINOR1), minor(f"{d['playCount']:,}", 12)], spacing=4, tight=True)
    local_dir: Optional[Path] = d["dir"]
    return ft.Container(
        content=ft.Row([
            swatch(d["bg"], 36),
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
    def build(a: App) -> ft.Control:
        tiles = []
        for cid in card_ids(deck_dir):
            cdir = deck_dir / "cards" / cid
            tiles.append(ft.Container(
                content=ft.Column([
                    ft.Container(width=104, height=104 * CARD_RATIO, bgcolor=card_bg(cdir), border_radius=12,
                                 border=ft.Border.all(1, SEP)),
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

    return Screen(title="Deck", build=build)


# ── card: list of blueprints ───────────────────────────────────────────────

def card_screen(app: App, deck_dir: Path, card_id: str) -> Screen:
    ctx = CardContext(app, deck_dir, card_id)
    card_dir = ctx.card_dir

    def build(a: App) -> ft.Control:
        from screens_blueprint import blueprint_screen
        bg = card_bg(card_dir)
        files = blueprint_files(card_dir)
        if not files:
            return ft.Container(minor("No blueprints", 13), alignment=ft.Alignment.CENTER, expand=True)
        entries = []
        for f in files:
            bp = read_json(f)
            entries.append((bp.get("title") or f.stem, f, bp))
        entries.sort(key=lambda t: t[0].lower())
        rows, thumb_jobs = [], []
        for name, f, bp in entries:
            holder = ft.Container(width=52, height=52, bgcolor=bg, border_radius=8, border=ft.Border.all(1, SEP),
                                  alignment=ft.Alignment.CENTER)
            d2 = drawing.get_drawing2(bp)
            if d2 and drawing.frame_count(d2):
                thumb_jobs.append((holder, f))
            else:
                holder.content = castle_icon("brick", 22, MINOR1)
            rows.append(ft.Container(
                content=ft.Row([holder, text(name, 15, medium=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS,
                                             expand=True)],
                               vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                padding=ft.Padding.symmetric(vertical=8, horizontal=14),
                border=ft.Border(bottom=ft.BorderSide(1, SEP)), ink=True,
                on_click=lambda e, ff=f: a.push(blueprint_screen(a, ctx, ff)),
            ))

        async def load_thumbs():
            import asyncio
            for holder, f in thumb_jobs:
                png = await asyncio.to_thread(_safe_thumb, f, bg)
                if png:
                    holder.content = ft.Image(src=png, width=48, height=48, fit=ft.BoxFit.CONTAIN,
                                              gapless_playback=True)
                    a.page.update()

        if thumb_jobs:
            a.page.run_task(load_thumbs)
        return ft.ListView(rows, spacing=0, expand=True)

    return Screen(title="Card", build=build, bar=lambda a: card_bar(a, ctx, "card"),
                  on_back=lambda: leave_card(app, ctx))
