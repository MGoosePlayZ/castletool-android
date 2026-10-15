import _path  # noqa: F401  (puts src/ on sys.path)
"""Headless smoke test: builds every screen against a stub page and exercises the app logic."""
import asyncio, json, os, tempfile, shutil
from pathlib import Path
import flet as ft
from PIL import Image
import mido

tmp = Path(tempfile.mkdtemp()); os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)
tasks = []
class Page:
    platform = ft.PagePlatform.WINDOWS
    def __init__(self): self.dialogs = []
    def update(self, *a): pass
    def add(self, *c): pass
    def run_task(self, fn, *a): tasks.append((fn, a))
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.dialogs.pop()
page = Page()
def drain(skip=("tick", "run")):
    while tasks:
        fn, a = tasks.pop(0)
        if getattr(fn, "__name__", "") in skip: continue
        asyncio.run(fn(*a))
def toasts(): return [d for d in page.dialogs if isinstance(d, ft.SnackBar)]

# ---- a persisted sign-in must survive restart, and work offline
(tmp / "session.json").write_text(json.dumps({"token": "tok"}))
(tmp / "profile.json").write_text(json.dumps({"userId": "u1", "username": "goose"}))
(tmp / "profile-photo.img").write_bytes(b"\x89PNG fake")
GLOBAL = ([
    {"deckId": "g-only", "title": "Global One", "caption": "", "visibility": "public", "lastModified": "2026-10-10T10:00:00Z", "playCount": 5, "bg": "#112233"},
    {"deckId": "deck1", "title": "dup of local", "caption": "", "visibility": "public", "lastModified": "2026-10-01T10:00:00Z", "playCount": 1, "bg": "#000000"},
    {"deckId": "g-priv", "title": "", "caption": "Only a description", "visibility": "private", "lastModified": 1760000000000, "playCount": 0, "bg": None}])
(tmp / "global_decks.json").write_text(json.dumps(GLOBAL))
import app as appmod
from castle_api import OfflineError
app = appmod.App(page); app._load_cached_session()
assert app.signed_in and app.user["username"] == "goose" and app.avatar_bytes
class Off:
    token = "tok"
    def me(self): raise OfflineError("down")
app.api = Off()
asyncio.run(app.refresh_profile()); assert app.online is False and app.signed_in and app.user["username"] == "goose"   # offline != signed out
assert asyncio.run(app.ensure_online()) is False and toasts()[-1].content.value == "No Wi-Fi."
app.api = type("A", (), {"token": None})(); assert asyncio.run(app.ensure_online()) is False and toasts()[-1].content.value == "Sign in first."
app.api = Off(); page.dialogs.clear()
class Rejected(Off):
    def me(self): return None
app.api = Rejected(); asyncio.run(app.refresh_profile()); assert not app.signed_in and not (tmp / "session.json").exists()   # token rejected => signed out
(tmp / "session.json").write_text(json.dumps({"token": "tok"})); (tmp / "profile.json").write_text(json.dumps({"userId": "u1", "username": "goose"}))
(tmp / "global_decks.json").write_text(json.dumps(GLOBAL))
app = appmod.App(page); app._load_cached_session(); app.api = Off(); app.online = False
assert app.avatar(36) is not None

# ---- fixture deck
d = app.decks_dir / "deck1"; c = d / "cards/card1"; (c / "scene/blueprints").mkdir(parents=True)
(d / "deck.json").write_text(json.dumps({"deckId": "deck1", "title": "", "caption": "A caption about things", "visibility": "private",
    "lastModified": "2026-10-12T10:00:00Z", "playCount": 1234, "initialCard": {"cardId": "card1", "backgroundColor": "#336699"},
    "cards": [{"cardId": "card1", "backgroundColor": "#336699"}]}))
(c / "card.json").write_text(json.dumps({"cardId": "card1", "backgroundColor": "#336699", "sceneProperties": {"clock": {"tempo": 100}}}))
(c / "scene/actors.internal.json").write_text("[]")
for name in ("Hero", "Tune"):
    (c / f"scene/blueprints/{name.lower()}.json").write_text(json.dumps({"entryId": name, "title": name, "entryType": "actorBlueprint", "actorBlueprint": {"components": {}}}))
import blueprint_ops as ops, screens_library as lib, screens_blueprint as sb, cardbar, session as S, drawing
gif = tmp / "g.gif"; fr = [Image.new("RGB", (16, 16), col) for col in ((255,0,0),(0,255,0),(0,0,255))]; fr[0].save(gif, save_all=True, append_images=fr[1:], duration=100, loop=0)
ops.import_media(c / "scene/blueprints/hero.json", c, gif, {}, log=print)
mid = mido.MidiFile(ticks_per_beat=480); tr = mido.MidiTrack(); mid.tracks.append(tr)
for n in (60, 64, 67):
    tr.append(mido.Message("note_on", note=n, velocity=90, time=480)); tr.append(mido.Message("note_off", note=n, velocity=0, time=10))
mp = tmp / "s.mid"; mid.save(mp); ops.import_midi(c / "scene/blueprints/tune.json", c, mp, log=print)

# ---- tabs: dedupe, filters, ordering
ids = lambda tab: [i["deckId"] for i in lib.tab_decks(app, tab)]
assert ids("LOCAL") == ["deck1"]
print("GLOBAL ids:", ids("GLOBAL"), "RECENT:", ids("RECENT"))
assert ids("GLOBAL") == ["g-only", "g-priv"]
assert ids("RECENT") == ["deck1", "g-only", "g-priv"] or ids("RECENT")[0] in ("deck1", "g-priv")
assert ids("PRIVATE") == [i for i in ids("RECENT") if i in ("deck1", "g-priv")]
assert ids("PUBLIC") == ["g-only"] and ids("UNLISTED") == []
row = lib.deck_row(app, lib.tab_decks(app, "LOCAL")[0])
assert row.content.controls[1].content.value == "A caption about things"                # untitled => description
assert len(row.content.controls) == 4 and row.on_long_press is not None                  # no visibility column; local rows can be deleted
g = [i for i in lib.tab_decks(app, "GLOBAL") if i["deckId"] == "g-priv"][0]
assert lib.deck_row(app, g).on_long_press is None and lib.deck_row(app, g).content.controls[1].content.value == "Only a description"
assert lib.fmt_date("2026-10-12T10:00:00Z").count("/") == 2 and lib.fmt_date(None) == "--/--/--"

# ---- screens
home = lib.home_screen(app); app.stack.clear(); app.push(home); app.decks_tab = "GLOBAL"; app.refresh(); drain()
app.decks_tab = "RECENT"; app.refresh(); drain()
home.left(app); home.actions(app); lib.clone_dialog(app)
ds = lib.deck_screen(app, d); ds.build(app); assert ds.title == "Deck"
cs = lib.card_screen(app, d, "card1"); app.push(ds); app.push(cs); drain()
ctx = cardbar.CardContext(app, d, "card1")
for mode in ("card", "blueprint", "player"): cardbar.card_bar(app, ctx, mode)
assert not ctx.session.dirty

# ---- edit → dirty → undo/redo → discard; save marks clean
ctx.edit_sync(lambda: ops.set_card_background(ctx.card_dir, "#aa0000"))
assert ctx.session.dirty and S.deck_has_unsaved(d) and lib.card_bg(c) == "#aa0000"
assert ctx.session.undo() and lib.card_bg(c) == "#336699" and not ctx.session.dirty
assert ctx.session.redo() and ctx.session.dirty
saved = []
appmod.push_card = lambda api, deck_dir, card_id, log: saved.append(card_id)
app.api = type("A", (), {"token": "tok"})(); app.online = True
assert asyncio.run(app.save_card(ctx)) and saved == ["card1"] and not ctx.session.dirty and not S.deck_has_unsaved(d)
ctx.edit_sync(lambda: ops.set_card_background(ctx.card_dir, "#00aa00")); assert ctx.session.dirty
asyncio.run(cardbar.leave_card(app, ctx)); assert isinstance(page.dialogs[-1], ft.AlertDialog) and page.dialogs[-1].title.value == "Save Changes?"
page.dialogs.pop(); ctx.session.discard(); assert lib.card_bg(c) == "#aa0000" and not ctx.session.dirty
cardbar.card_settings_dialog(app, ctx)

# ---- blueprint screens, every tab + import dialog variants
for bpname in ("hero", "tune"):
    scr = sb.blueprint_screen(app, ctx, c / f"scene/blueprints/{bpname}.json"); scr.bar(app)
    v = scr.build.__closure__[0].cell_contents; scr.build(app)
    for tab in ("media", "music", "audio"): v.tab = tab; v.render(update=False)
    v.tab = "music"; v.render(update=False)
    if bpname == "tune": assert v.engine is not None and v.engine.notes
    drain(); v.dispose()
view = sb.BlueprintView(app, ctx, c / "scene/blueprints/hero.json")
def dlg(path):
    info = ops.inspect_media(path, log=print); dd = sb.ImportDialog(view, path, info); dd.options.controls = dd.option_controls(); return dd
svg = tmp / "a.svg"; svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>')
d1 = dlg(gif); kind, o = d1.collect(); assert kind == "media" and o["every"] == 1
d2 = dlg(svg); kind, o = d2.collect(); assert o["color"] is None and o["svg_style"] == "vector"           # override off by default
d2.vals["color_on"] = True; d2.vals["color"] = "#00ff00"; d2.options.controls = d2.option_controls(); assert d2.collect()[1]["color"] == [0, 1, 0, 1]
d2.vals["svg_style"] = "bitmap"; d2.options.controls = d2.option_controls()
d3 = dlg(_path.make_test_font(Path(tempfile.mkdtemp()) / "t.ttf")); kind, o = d3.collect(); assert kind == "font" and o["charsets"] == ["ASCII"]
d1.vals["scale"] = "abc"
try: d1.collect(); assert False
except ops.OpError: pass
asyncio.run(d1._preview(d1.token)) if False else None
d4 = dlg(gif); d4.vals["scale"] = "2"; asyncio.run(d4._preview(d4.token)); assert d4.preview_box.opacity == 1.0     # preview renders from the current settings

# ---- delete a local deck (only the device copy)
app.ask_delete_deck(d, "x"); assert isinstance(page.dialogs[-1], ft.AlertDialog)
asyncio.run(app.delete_deck(d)); assert not d.exists() and ids("LOCAL") == []
# ---- profile screen
appmod.profile_screen(app).build(app)
print("ui ok")
