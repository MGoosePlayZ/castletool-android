import _path  # noqa: F401  (puts src/ on sys.path)
"""Headless test: the Castle deck list loads in pages (state in App, rows added in place by the Decks screen)."""
import asyncio, json, os, tempfile
from pathlib import Path
import flet as ft

tmp = Path(tempfile.mkdtemp()); os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)
tasks = []
class Page:
    platform = ft.PagePlatform.WINDOWS
    height = 800
    def __init__(self): self.dialogs = []
    def update(self, *a): pass
    def add(self, *c): pass
    def run_task(self, fn, *a): tasks.append((fn, a))
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.dialogs.pop()
page = Page()
def drain():
    while tasks:
        fn, a = tasks.pop(0)
        asyncio.run(fn(*a))

import app as appmod, screens_library as lib
from castle_api import OfflineError

TOTAL = 75
def deck(i):
    return {"deckId": f"d{i:03d}", "title": f"Deck {i}", "visibility": "public", "caption": "", "playCount": i, "bg": "#112233",
            "lastModified": f"2026-10-{28 - i // 24:02d}T{23 - i % 24:02d}:00:00Z"}
ALL = [deck(i) for i in range(TOTAL)]                       # newest first, like `recent_edited`
asked: list[int] = []
cap = [10_000]                                              # the most decks the fake Castle ever returns
fail = [False]
def fake_list(api, limit, user_id):
    asked.append(limit)
    if fail[0]:
        raise OfflineError("down")
    return ALL[:min(limit, cap[0])]
appmod.list_recent_decks = fake_list

(tmp / "session.json").write_text(json.dumps({"token": "tok"}))
(tmp / "profile.json").write_text(json.dumps({"userId": "u1", "username": "goose"}))
app = appmod.App(page); app._load_cached_session()
assert app.global_limit == appmod.GLOBAL_PAGE == 30 and app.global_decks == [] and not app.global_more

# ---- pages: 30, 60, then the last 15 (75 < 90 means nothing more)
assert asyncio.run(app.refresh_global(force=True, repaint=False)) and len(app.global_decks) == 30 and app.global_more
assert asyncio.run(app.load_more_global()) and len(app.global_decks) == 60 and app.global_limit == 60 and app.global_more
assert asyncio.run(app.load_more_global()) and len(app.global_decks) == 75 and app.global_limit == 90 and not app.global_more
assert asked == [30, 60, 90]
assert asyncio.run(app.load_more_global()) is False and asked == [30, 60, 90]           # nothing more: no request
assert [d["deckId"] for d in app.global_decks] == [d["deckId"] for d in ALL]
assert len(json.loads((tmp / "global_decks.json").read_text())) == 75                   # the cache holds everything loaded

# ---- a refresh keeps the loaded decks (asks for as many as are loaded), it never shortens the list
app.global_more = True; app.global_limit = 60; app.global_decks = app.global_decks[:60]
assert asyncio.run(app.refresh_global(force=True, repaint=False)) and asked[-1] == 60 and len(app.global_decks) == 60

# ---- a restart restores the page size from the cache
(tmp / "global_decks.json").write_text(json.dumps(ALL))
app2 = appmod.App(page); app2._load_cached_session()
assert len(app2.global_decks) == 75 and app2.global_limit == 90 and app2.global_more

# ---- a Castle that caps the list: no growth means stop asking
app3 = appmod.App(page); app3._load_cached_session(); app3.global_decks = []; app3.global_limit = 30; app3.global_more = False
cap[0] = 40; asked.clear()
assert asyncio.run(app3.refresh_global(force=True, repaint=False)) and len(app3.global_decks) == 30 and app3.global_more
assert asyncio.run(app3.load_more_global()) and len(app3.global_decks) == 40 and not app3.global_more      # 40 < 60
cap[0] = 10_000

# ---- offline: nothing changes, the flag is released, the app knows it is offline
app4 = appmod.App(page); app4._load_cached_session(); app4.global_decks = ALL[:30]; app4.global_limit = 30; app4.global_more = True
fail[0] = True
assert asyncio.run(app4.load_more_global()) is False
assert len(app4.global_decks) == 30 and app4.global_limit == 30 and app4.global_more and not app4.global_loading and not app4.online
fail[0] = False

# ---- the Decks screen adds the rows in place when the list is scrolled to its end
app5 = appmod.App(page); app5._load_cached_session()
app5.global_decks = ALL[:30]; app5.global_limit = 30; app5.global_more = True; app5.global_loaded_at = 10 ** 9     # no refresh on build
import time; app5.global_loaded_at = time.monotonic()
asked.clear()
home = lib.home_screen(app5); col = home.build(app5); drain()
list_host = col.controls[2]; lv = list_host.content
assert isinstance(lv, ft.ListView) and len(lv.controls) == 30 + 2                       # rows + bottom spinner + spacer
def scroll(pixels, kind=ft.ScrollType.UPDATE):
    lv.on_scroll(ft.OnScrollEvent(name="scroll", control=lv, event_type=kind, pixels=pixels, min_scroll_extent=0,
                                  max_scroll_extent=2000, viewport_dimension=700))
scroll(500); drain()
assert asked == [] and len(lv.controls) == 32                                           # far from the end: nothing loads
scroll(1800); drain()
assert asked == [60] and list_host.content is lv and len(lv.controls) == 60 + 2          # same ListView (scroll position kept)
assert app5.global_loading is False and not lv.controls[-2].visible
scroll(1900); drain()
assert asked == [60, 90] and len(lv.controls) == 75 + 2 and not app5.global_more
scroll(1990); drain()
assert asked == [60, 90]                                                                # the end: no more requests

# ---- a short tab keeps loading until it fills the screen or the decks run out
app6 = appmod.App(page); app6._load_cached_session()
app6.global_decks = ALL[:30]; app6.global_limit = 30; app6.global_more = True; app6.decks_tab = "PRIVATE"
app6.global_loaded_at = time.monotonic(); asked.clear()
home = lib.home_screen(app6); home.build(app6); drain()
assert asked == [60, 90] and len(app6.global_decks) == 75 and not app6.global_more      # no private decks at all: everything was loaded

# ---- LOCAL never asks Castle for more
app7 = appmod.App(page); app7._load_cached_session()
app7.global_decks = ALL[:30]; app7.global_limit = 30; app7.global_more = True; app7.decks_tab = "LOCAL"
app7.global_loaded_at = time.monotonic(); asked.clear()
home = lib.home_screen(app7); home.build(app7); drain()
assert asked == []

print("deck pages OK")
