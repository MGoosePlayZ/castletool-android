import _path  # noqa: F401
"""Bulk deck select: selection state, selection bar, set visibility, delete (single + typed username)."""
import asyncio, json, os, tempfile
from pathlib import Path
from types import SimpleNamespace
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
(tmp / "session.json").write_text(json.dumps({"token": "tok"}))
(tmp / "profile.json").write_text(json.dumps({"userId": "u1", "username": "Goose"}))
GLOBAL = [{"deckId": f"d{i}", "title": f"Deck {i}", "caption": "", "visibility": "public",
           "lastModified": "2026-10-10T10:00:00Z", "playCount": 0, "bg": "#000000"} for i in range(3)]
(tmp / "global_decks.json").write_text(json.dumps(GLOBAL))
import app as appmod
import bulk_decks as bd
import screens_library as lib

app = appmod.App(page); app._load_cached_session()
app.stack.clear(); app.push(lib.home_screen(app)); app.online = True
local = app.decks_dir / "d0"
(local / "cards").mkdir(parents=True)
(local / "deck.json").write_text(json.dumps({"deckId": "d0", "title": "Deck 0", "visibility": "public", "cards": []}))

calls = []
def set_vis(did, vis, title="", caption=""):
    calls.append(("vis", did, vis)); return vis
def del_deck(did):
    if did == "d2": raise RuntimeError("nope")
    calls.append(("del", did))
app.api = SimpleNamespace(token="tok", set_deck_visibility=set_vis, delete_deck=del_deck)
app.log = lambda *a, **k: None
bd.PAUSE = 0

# selection + bar
assert bd.selection_bar(app) is None and app.stack[-1].bar(app) is None
bd.toggle(app, "d0", "Deck 0"); bd.toggle(app, "d1", "Deck 1")
assert set(app.deck_sel) == {"d0", "d1"} and app.stack[-1].bar(app) is not None
bd.toggle(app, "d1", "Deck 1"); assert set(app.deck_sel) == {"d0"}
row = lib.deck_row(app, lib.tab_decks(app, "RECENT")[0], True)
assert row.bgcolor == lib.MAJOR and row.on_long_press is not None

# visibility
bd.toggle(app, "d1", "Deck 1")
asyncio.run(bd.set_visibility(app, ["d0", "d1"], "private"))
assert calls == [("vis", "d0", "private"), ("vis", "d1", "private")] and not app.deck_sel
assert json.loads((local / "deck.json").read_text())["visibility"] == "private"
assert [g["visibility"] for g in app.global_decks if g["deckId"] in ("d0", "d1")] == ["private", "private"]

# delete: one fails, the others go (local copy + cached entry)
calls.clear()
for d in ("d0", "d1", "d2"): bd.toggle(app, d, d)
asyncio.run(bd.delete_decks(app, ["d0", "d1", "d2"]))
assert calls == [("del", "d0"), ("del", "d1")] and not local.exists()
assert [g["deckId"] for g in app.global_decks] == ["d2"]

# multi-delete flow: warning → username gate (case-insensitive, button disabled until it matches)
for d in ("a", "b"): bd.toggle(app, d, d)
page.dialogs.clear(); bd.delete_selected(app)
dlg = page.dialogs[-1]
assert "dangerous" in dlg.content.value and [b.content.value for b in dlg.actions] == ["Go Back", "I understand and accept these risks"]
dlg.actions[1].on_click(None)
dlg2 = page.dialogs[-1]
field, go = dlg2.content.controls[1], dlg2.actions[1]
assert go.disabled and go.content.value == "Delete 2 Decks"
field.value = "goos"; field.on_change(None); assert go.disabled
field.value = "goose"; field.on_change(None); assert not go.disabled
print("bulk ok")
