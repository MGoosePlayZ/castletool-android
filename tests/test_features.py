import _path  # noqa: F401  (puts src/ on sys.path)
"""Cancel/progress, single-frame preview, local icons, color picker, profile/dev settings."""
import asyncio, json, os, tempfile, time, threading
from pathlib import Path
import flet as ft
import numpy as np
from PIL import Image

tmp = Path(tempfile.mkdtemp()); os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)
import tasks, blueprint_ops as ops, media_av, drawing, icons

# ---- cancel + progress plumbing
t = tasks.Task("x"); seen = []
t._on_update = lambda tk: seen.append(round(tk.frac or 0, 2))
with tasks.use(t):
    with tasks.span(0.5, 1.0):
        tasks.step(1, 2, "half")
assert seen and abs(seen[-1] - 0.75) < 1e-6 and t.message == "half"
t.cancel()
try:
    with tasks.use(t): tasks.check(); assert False
except tasks.Cancelled: pass

# ---- gif: only the asked frame is decoded; a cancelled import writes nothing
fr = [Image.new("RGB", (16, 16), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (9, 9, 9))]
gif = tmp / "a.gif"; fr[0].save(gif, save_all=True, append_images=fr[1:], duration=100, loop=0)
info = ops.inspect_media(gif, log=print)
assert info["category"] == "animation" and info["frames"] == 4
assert ops.preview_frame_count(info, {"every": 1}) == 4 and ops.preview_frame_count(info, {"every": 2}) == 2
png1 = ops.preview_media(gif, {}, log=print, frame=1); png2 = ops.preview_media(gif, {}, log=print, frame=2)
assert png1 and png2 and png1 != png2

# ---- video: one frame via seek, never the whole file
import av
vid = tmp / "v.mp4"
with av.open(str(vid), "w") as out:
    st = out.add_stream("mpeg4", rate=10); st.width, st.height, st.pix_fmt = 64, 48, "yuv420p"
    for i in range(60):
        a = np.full((48, 64, 3), (i * 4) % 255, dtype="uint8")
        for p in st.encode(av.VideoFrame.from_ndarray(a, format="rgb24")): out.mux(p)
    for p in st.encode(None): out.mux(p)
vi = ops.inspect_media(vid, log=print); assert vi["category"] == "video" and vi["frames"] >= 50, vi
t0 = time.time(); p10 = ops.preview_media(vid, {}, log=print, frame=10); p50 = ops.preview_media(vid, {}, log=print, frame=50)
assert p10 and p50 and p10 != p50 and time.time() - t0 < 10
# cancelled mid-import: nothing written
tk = tasks.Task("imp"); tk.cancel()
bp = tmp / "bp.json"; card = tmp / "card"; card.mkdir()
bp.write_text(json.dumps({"entryId": "a", "title": "A", "actorBlueprint": {"components": {}}}))
before = bp.read_text()
try: ops.import_media(bp, card, vid, {}, log=print, task=tk); assert False
except tasks.Cancelled: pass
assert bp.read_text() == before

# ---- real import reports progress up to the end
tk = tasks.Task("imp"); fr_seen = []; msgs = []
tk._on_update = lambda x: fr_seen.append(x.frac); tk._on_message = msgs.append; tk._min_interval = 0
ops.import_media(bp, card, gif, {"quantize": 8}, log=print, task=tk)
assert fr_seen and max(f for f in fr_seen if f is not None) > 0.5 and any("frame" in m.lower() for m in msgs), msgs

# ---- icons: 128px, initial frame, outside the deck folder, regenerated when the file changes
decks = tmp / "decks"; deck = decks / "deckX"; cdir = deck / "cards" / "c1"; (cdir / "scene/blueprints").mkdir(parents=True)
(deck / "deck.json").write_text("{}")
(cdir / "scene/blueprints/hero.json").write_text(bp.read_text())
store = icons.IconStore(decks)
f = cdir / "scene/blueprints/hero.json"
m = store.meta(cdir, f); assert m["has_drawing"] and not m["done"] and store.png(cdir, f) is None
data = store.render(cdir, f); im = Image.open(__import__("io").BytesIO(data)); assert im.size == (128, 128)
assert store.png(cdir, f) == data and (decks / "deckX-icons" / "c1" / "hero.png").exists()
assert not list(deck.rglob("*.png"))                    # nothing inside the deck folder
import shutil; shutil.copy(bp, f); f.touch(); assert store.peek(cdir, f) is None   # edited → stale
store.delete_deck(deck); assert not (decks / "deckX-icons").exists()

# ---- UI pieces
class Page:
    platform = ft.PagePlatform.WINDOWS; height = 800
    def __init__(self): self.dialogs = []; self.t = []
    def update(self, *a): pass
    def add(self, *c): pass
    def run_task(self, fn, *a): self.t.append((fn, a))
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.dialogs.pop()
page = Page()
import app as appmod, widgets
app = appmod.App(page)
# color picker: opens, wheel + sliders respond, OK returns hex
got = []
widgets.open_color_picker(app, "#336699", got.append)
dlg = page.dialogs[-1]; body = dlg.content
class Ev:  # fake pointer event
    def __init__(s, x, y): s.local_position = type("P", (), {"x": x, "y": y})()
wheel = body.controls[1].content
wheel.on_tap_down(Ev(120, 8)); wheel.on_pan_end(None)             # top of the ring → hue 270
wheel.on_tap_down(Ev(120, 120)); wheel.on_pan_update(Ev(150, 100)); wheel.on_pan_end(None)
assert dlg.title is None and not dlg.actions
assert abs(float(widgets.norm_hex("#abc") == "#aabbcc"))
tabs = body.controls[2]; tabs.controls[1].on_click(None)            # HSV tab
body.controls[3].controls[0]                                        # H slider exists
sl = body.controls[3].controls[0].controls[1]; sl.on_pan_update(Ev(100, 10)); sl.on_tap_down(Ev(0, 10))
hexf = body.controls[4].controls[0]; hexf.value = "#ff0000"; hexf.on_change(None)
ok = body.controls[4].controls[1]; ok.on_click(None)
assert got == ["#ff0000"], got

# progress overlay + work(): percentage, status line, cancel
async def scenario():
    def slow(task):
        for i in range(200):
            tasks.step(i, 200, f"Processing frame {i + 1}/200"); time.sleep(0.01)
        return True
    async def canceller():
        await asyncio.sleep(0.3)
        pv = [d for d in page.dialogs if isinstance(d, ft.AlertDialog) and d.modal][-1]
        pv.content.content.controls[-1].on_click(None)             # the overlay's Cancel
    asyncio.get_event_loop().create_task(canceller())
    return await app.work("Test", slow)
page.dialogs.clear()
assert asyncio.run(scenario()) is None and app.last_work_cancelled
assert any("cancelled" in l for _t, _v, l in app.log_entries)
async def fine():
    return await app.work("Quick", lambda task: (tasks.step(1, 2, "half") or True))
assert asyncio.run(fine()) is True

# profile screen: dev mode off hides logs; on shows verbosity, save, copy
prof = appmod.profile_screen(app); col = prof.build(app)
assert app.log_view().height == 0
app.set_setting("dev_mode", True); app.log("hello"); app.log("noisy", 2); app.log("error: bad")
box = app.log_view(); texts = [c.value for c in box.content.controls[0].content.controls]
assert "hello" in texts and "error: bad" in texts and "noisy" not in texts       # Normal hides verbose
app.set_setting("log_level", 2); assert "noisy" in [c.value for c in app.log_view().content.controls[0].content.controls]
assert "[VERBOSE] noisy" in app.log_text() and "[ERROR] error: bad" in app.log_text()
prof.build(app)
assert not isinstance(app.avatar(88, ring=False).border, ft.Border) and app.avatar(36).padding == 1
print("features ok")
