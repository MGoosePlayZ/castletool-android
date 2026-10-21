import _path  # noqa: F401  (puts src/ on sys.path)
"""Headless test: text-media tab, card settings auto-apply, first-run screens, deleted decks."""
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
import app as appmod, widgets, theme
import blueprint_ops as ops, screens_library as lib, screens_blueprint as sb, cardbar
from castle_api import CastleAPI, CastleAPIError, DeckDeleted

# ---- contrast background: light text on black, dark text on white
assert widgets.contrast_bg("#ffffff") == "#000000" and widgets.contrast_bg("#000000") == "#ffffff"
assert widgets.contrast_bg("#ffff00") == "#000000" and widgets.contrast_bg("#0000ff") == "#ffffff"

# ---- fonts: bundled DMSans keeps its name, an absent Castle font falls back to the body font
assert theme.text_font_family("DMSans") == "DMSans"
assert theme.text_font_family("Bore") in ("Bore", theme.BODY)
(theme.ASSETS / "fonts" / "Tektur.ttf").write_bytes(b"x")
try:
    assert theme.text_font_family("Tektur") == "Tektur" and theme.font_map()["Tektur"] == "fonts/Tektur.ttf"
finally:
    (theme.ASSETS / "fonts" / "Tektur.ttf").unlink()
assert theme.text_font_family("Tektur") == theme.BODY and "Tektur" not in theme.text_font_files() or not theme.ASSETS.exists()

# ---- first run: gate until the first sign-in, then never again
app = appmod.App(page); app._load_cached_session()
assert not app.signed_in and app.needs_signin_gate
async def go():
    await app.start()
    assert app.stack[0].title == "Sign in" and app.stack[0].bar(app).height == 0
    app.stack[0].build(app)
    class Api:
        token = None
        def login_password(self, u, p): self.token = "tok"; return {"userId": "u1", "username": "goose"}
        def me(self): return {"userId": "u1", "username": "goose"}
    app.api = Api()
    async def noop(*a, **k): return None
    app.refresh_global = noop; app.refresh_profile = noop
    assert await app.sign_in_password("goose", "pw")
asyncio.run(go())
app.set_setting("onboarded", True)
from screens_library import home_screen
app.replace_root(home_screen(app))                                 # sign-in goes straight to Decks
assert len(app.stack) == 1 and app.stack[0].title == "Decks" and app.settings["onboarded"] is True
app.api.token = None; app.user = None                              # signed out later: no gate, normal app
assert not app.needs_signin_gate
assert json.loads((tmp / "settings.json").read_text())["onboarded"] is True
app2 = appmod.App(page); app2._load_cached_session(); assert not app2.needs_signin_gate   # persisted

# an already signed-in user (token saved) skips the first-run screens too
tmp2 = Path(tempfile.mkdtemp()); (tmp2 / "session.json").write_text(json.dumps({"token": "t"}))
os.environ["FLET_APP_STORAGE_DATA"] = str(tmp2); app3 = appmod.App(page); app3._load_cached_session()
assert app3.signed_in and not app3.needs_signin_gate and app3.settings["onboarded"] is True
os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)

# ---- decks page when signed out
home = lib.home_screen(app); app.stack.clear(); app.push(home)
def empty_msg():
    lv = lib.home_screen  # noqa
    col = app.body.content
    return repr(col)
assert "You are not signed in" in empty_msg() and "No decks" not in empty_msg()
app.api.token = "tok"; app.refresh()
assert "No decks" in empty_msg()
app.api.token = None

# ---- fixtures: a deck with a text blueprint
d = app.decks_dir / "deck1"; c = d / "cards/card1"; (c / "scene/blueprints").mkdir(parents=True)
(d / "deck.json").write_text(json.dumps({"deckId": "deck1", "title": "T", "caption": "", "visibility": "private",
    "lastModified": "2026-10-12T10:00:00Z", "playCount": 0, "initialCard": {"cardId": "card1", "backgroundColor": "#336699"},
    "cards": [{"cardId": "card1", "backgroundColor": "#336699"}]}))
(c / "card.json").write_text(json.dumps({"cardId": "card1", "backgroundColor": "#336699", "sceneProperties": {"clock": {"tempo": 100}}}))
(c / "scene/actors.internal.json").write_text("[]")
bp = c / "scene/blueprints/words.json"
bp.write_text(json.dumps({"entryId": "W", "title": "Words", "entryType": "actorBlueprint", "actorBlueprint": {"components": {"Text": {
    "content": "hi", "fontName": "Tektur", "alignment": "right", "verticalAlignment": "bottom",
    "color": {"r": 1, "g": 1, "b": 0, "a": 1}, "disabled": False}}}}))
ctx = cardbar.CardContext(app, d, "card1")

# ---- text media tab
view = sb.BlueprintView(app, ctx, bp)
panel = view.text_panel()
field, fonts, align_row, color = panel.controls[:4]
assert panel.horizontal_alignment == ft.CrossAxisAlignment.STRETCH
assert field.bgcolor == "#000000" and field.text_style.color == "#ffff00"            # yellow text → black box
assert field.text_style.font_family == theme.text_font_family("Tektur")
assert len(fonts.controls) == len(ops.TEXT_FONTS) and all(b.aspect_ratio == 1 and b.expand == 1 for b in fonts.controls)
assert align_row.controls[1].value == "Bottom Right"
color.on_click(None); dlg = page.dialogs[-1]; hexf = dlg.content.controls[4].controls[0]
hexf.value = "#112233"; hexf.on_change(None)
dlg.content.controls[0].controls[0].on_click(None)                                      # the X applies it
assert ops.text_color_hex(ops.load_blueprint(bp)["actorBlueprint"]["components"]["Text"]["color"]) == "#112233"
assert field.bgcolor == "#ffffff" and field.text_style.color == "#112233"             # dark text → white box
view.dispose()

# ---- card settings: no Save, changes land in the buffer
cardbar.card_settings_dialog(app, ctx); dlg = page.dialogs[-1]
assert dlg.actions[0].content.value == "OK"
col = dlg.content.controls
swatch_row = col[0]
lib_color = swatch_row.controls[1]                                                     # ColorField control
lib_color.on_click(None); pk = page.dialogs[-1]; hexf = pk.content.controls[4].controls[0]
hexf.value = "#aa0000"; hexf.on_change(None); pk.content.controls[4].controls[1].on_click(None)
assert lib.card_bg(c) == "#aa0000" and ctx.session.dirty                              # applied without pressing OK
dlg.actions[0].on_click(None)                                                          # OK closes (tempo untouched)
assert ops.card_tempo(c) == 100

# ---- deleted deck: modal, local copy removed (unsaved edits would make open_deck use the local copy)
ctx.session.discard()
class Gone:
    token = "tok"
    def deck(self, i): raise DeckDeleted("You do not have permission to view this deck")
async def gone():
    app.api = Gone(); app.online = True
    app.global_decks = [{"deckId": "deck1", "title": "T"}]
    page.dialogs.clear()
    await app.open_deck("deck1", d)
asyncio.run(gone())
assert not d.exists() and app.global_decks == []
last = page.dialogs[-1]
assert last.title.controls[-1].value == "This deck was deleted" and last.actions[0].content.value == "OK"
last.actions[0].on_click(None); assert not [x for x in page.dialogs if isinstance(x, ft.AlertDialog)]

# ---- the API maps Castle's message to DeckDeleted, other errors pass through
api = CastleAPI("t")
def raising(msg):
    def f(*a, **k): raise CastleAPIError(msg)
    return f
api._call = raising("You do not have permission to view this deck")
try: api.deck("x"); raise SystemExit("no error")
except DeckDeleted: pass
api._call = raising("boom")
try: api.deck("x"); raise SystemExit("no error")
except DeckDeleted: raise SystemExit("wrong error")
except CastleAPIError: pass
print("polish ok")
