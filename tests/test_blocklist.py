import _path  # noqa: F401
"""Blocklist: parsing, matching, caching, and the blocked screen."""
import asyncio, json, os, tempfile
from pathlib import Path
import flet as ft
import blocklist as bl

DATA = {"message": "Blocked: $1\nBe nicer.", "list": {"BadUser": "bigotry", "x": "y"}}
d = bl._clean(DATA)
assert bl.message_for(d, "baduser") == "Blocked: bigotry\nBe nicer." and bl.message_for(d, "@BADUSER ")
assert bl.message_for(d, "gooduser") is None and bl.message_for(None, "baduser") is None and bl.message_for(d, "") is None
assert bl._clean({"list": []}) is None and bl._clean([]) is None

tmp = Path(tempfile.mkdtemp())
class R:
    def __init__(s, j, ok=True): s.j, s.ok = j, ok
    def raise_for_status(s):
        if not s.ok: raise RuntimeError("http")
    def json(s): return s.j
calls = []
def fake(url, **kw):
    calls.append(url); return R(DATA)
bl.requests.get = fake
bl._mem.update(t=0.0, data=None)
assert bl.load(tmp / "c.json", "u") == d and (tmp / "c.json").exists()
bl.load(tmp / "c.json", "u"); assert len(calls) == 1                       # reused within the TTL
def down(url, **kw): raise OSError("offline")
bl.requests.get = down; bl._mem.update(t=0.0, data=None)
assert bl.load(tmp / "c.json", "u") == d                                    # falls back to the cached copy
bl._mem.update(t=0.0, data=None); assert bl.load(tmp / "none.json", "u") is None   # no copy: nobody blocked

# the app: a blocked account sees only the notice + close button, and stays signed in
os.environ["FLET_APP_STORAGE_DATA"] = str(tmp / "app")
(tmp / "app").mkdir()
(tmp / "app" / "session.json").write_text(json.dumps({"token": "t"}))
(tmp / "app" / "profile.json").write_text(json.dumps({"userId": "u", "username": "baduser"}))
class Page:
    platform = ft.PagePlatform.WINDOWS; height = 800
    def update(self, *a): pass
    def add(self, *c): pass
    def run_task(self, fn, *a): pass
    def show_dialog(self, d): pass
import app as appmod
a = appmod.App(Page()); a._load_cached_session(); a.push(appmod.Screen(title="Decks"))
bl.requests.get = fake; bl._mem.update(t=0.0, data=None)
asyncio.run(a.check_blocked())
assert a.blocked_msg == "Blocked: bigotry\nBe nicer." and a.signed_in
col = a.body.content.content
assert col.controls[0].value == a.blocked_msg and col.controls[2].content.controls[0].value == "Close app"
a.refresh_body(); a.refresh_bar(); a._show()
assert a.body.content.content.controls[0].value == a.blocked_msg and a.signed_in
print("blocklist ok")
