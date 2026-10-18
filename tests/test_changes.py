import _path  # noqa: F401
"""octave shift, cauldron decks, upload limit, video-sound warnings, layer UI, back button."""
import asyncio, json, os, tempfile
from pathlib import Path
import flet as ft
import numpy as np
from PIL import Image

tmp = Path(tempfile.mkdtemp()); os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)

# ---- instrument root note (midiNote) shifts that instrument's notes
import synth
def music(root_a, root_b):
    mk = lambda root: {"instrument": {"type": "sampler", "props": {"volume": 1, "muted": False},
                                      "sample": {"type": "tone", "waveform": "sine", "attack": 0, "release": 0.4, "midiNote": root,
                                                 "playbackRate": {"value": 1}, "amplitude": {"value": 1}}}, "sequence": {}}
    return {"song": {"patterns": {}, "tracks": [mk(root_a), mk(root_b)]}}
voices, _ = synth.voices_from_music(music(48, 60))
assert voices[0]["shift"] == 0 and voices[1]["shift"] == 12
def f0(buf, sr=22050):                              # dominant frequency
    spec = np.abs(np.fft.rfft(buf * np.hanning(len(buf)))); return np.argmax(spec) * sr / len(buf)
a, b = f0(synth.note_buffer(voices[0], 60, 22050)), f0(synth.note_buffer(voices[1], 60, 22050))
assert abs(b / a - 2) < 0.05, (a, b)                # midiNote 60 = one octave above 48
v2, _ = synth.voices_from_music(music(36, None)); assert v2[0]["shift"] == -12 and v2[1]["shift"] == 0   # missing/odd value = default

# ---- cauldron detection
import deck_store as ds
assert ds.scene_uses_cauldron({"snapshot": {"x": [{"a": {"experimentalWeb": {"enabled": True, "url": "x"}}}]}})
assert not ds.scene_uses_cauldron({"experimentalWeb": {"enabled": False}}) and not ds.scene_uses_cauldron({"a": [1, "experimentalWeb"]})
deck = tmp / "deckC"; deck.mkdir()
(deck / "deck.json").write_text(json.dumps({"deckId": "deckC", "initialCard": {"cardId": "c1", "sceneDataUrl": "http://s/1"}, "cards": []}))
class Api:
    calls = 0; data = {"experimentalWeb": {"enabled": True}}
    def download_scene_data(self, url): Api.calls += 1; return Api.data
cache = tmp / "cache" / "cauldron.json"
assert ds.cauldron_status(cache, deck) == (None, True)
assert ds.detect_cauldron(Api(), deck, cache) is True and Api.calls == 1
assert ds.detect_cauldron(Api(), deck, cache) is True and Api.calls == 1              # cached: no second download
assert ds.cauldron_status(cache, deck) == (True, False)
(deck / "deck.json").write_text(json.dumps({"deckId": "deckC", "initialCard": {"cardId": "c1"}, "cards": []}))     # url gone after a push: keep the answer
assert ds.cauldron_status(cache, deck) == (True, False)
(deck / "deck.json").write_text(json.dumps({"deckId": "deckC", "initialCard": {"cardId": "c1", "sceneDataUrl": "http://s/2"}, "cards": []}))
Api.data = {"snapshot": {}}; assert ds.detect_cauldron(Api(), deck, cache) is False and Api.calls == 2          # new scene data: asked again
class Down(Api):
    def download_scene_data(self, url): raise OSError("offline")
(deck / "deck.json").write_text(json.dumps({"deckId": "deckC", "initialCard": {"cardId": "c9", "sceneDataUrl": "http://s/9"}, "cards": []}))
try: ds.detect_cauldron(Down(), deck, cache); raise SystemExit("offline passed")
except OSError: pass
assert "c9" not in json.loads(cache.read_text())                                       # failures aren't cached

# ---- upload limit (per card: each card is its own upload) + readable HTTP errors
import castle_api as ca
assert ca.http_error_message(413, "<html>big</html>") == ca.TOO_LARGE_MESSAGE
assert ca.http_error_message(502, "<html><body>Bad gateway</body></html>") == "Castle returned an error (HTTP 502)."
assert "<" not in ca.http_error_message(500, "<Error><Code>x</Code></Error>")
big = tmp / "bigdeck"
for cid, n in (("a", 6), ("b", 6)):                                                      # two 6 MB cards: 12 MB together
    cd = big / "cards" / cid; (cd / "scene/blueprints").mkdir(parents=True)
    (cd / "scene/actors.internal.json").write_text("[]")
    (cd / "scene/blueprints/x.json").write_text(json.dumps({"entryId": "x" + cid, "pad": "z" * (n * 1024 * 1024)}))
(big / "deck.json").write_text(json.dumps({"deckId": "big", "cards": [{"cardId": "a"}, {"cardId": "b"}], "initialCard": {"cardId": "a"}}))
size = ds.deck_upload_size(big); assert 12 * 1048576 < size < 12.1 * 1048576, size
assert 6 * 1048576 < ds.card_upload_size(big / "cards/a") < 6.1 * 1048576
ds.assert_deck_fits(big)                                                                 # 12 MB in total is fine: no single card is over 10 MB
class Rec:
    def __init__(self): self.up = []; self.saved = []
    def upload_scene_data(self, cid, sd): self.up.append(cid); return "up-" + cid
    def update_card_and_deck_v2(self, d, c):
        self.saved.append(c["cardId"]); return {"deck": {"deckId": d["deckId"]}, "card": {"cardId": c["cardId"]}}
    def card(self, cid): return None
r = Rec(); ds.push_card(r, big, "a"); assert r.up == ["a"]                               # only card a is measured and sent
r = Rec(); ds.push_deck(r, big, log=lambda m: None); assert r.up == ["a", "b"] and r.saved == ["a", "b"]
(big / "cards/b/scene/blueprints/x.json").write_text(json.dumps({"entryId": "xb", "pad": "z" * (11 * 1024 * 1024)}))
try: ds.assert_card_fits(big / "cards/b"); raise SystemExit("11 MB card accepted")
except ca.UploadTooLarge as ex: assert "Card b" in str(ex) and "11.0 MB" in str(ex) and "10 MB" in str(ex), ex
class Net:
    def __getattr__(self, n): raise SystemExit("network touched: " + n)
try: ds.push_deck(Net(), big); raise SystemExit("pushed")
except ca.UploadTooLarge: pass                                                           # refused before any network call
try: ds.push_card(Net(), big, "b"); raise SystemExit("pushed")
except ca.UploadTooLarge: pass
r = Rec(); ds.push_card(r, big, "a"); assert r.up == ["a"]                               # card a is still fine on its own

# ---- video with sound: failures are reported, not silent
import blueprint_ops as ops, castletool_core as ct, media_av, av
vid = tmp / "v.mp4"
out = av.open(str(vid), "w"); vs = out.add_stream("mpeg4", rate=10); vs.width = vs.height = 64; vs.pix_fmt = "yuv420p"
au = out.add_stream("aac", rate=44100); au.layout = "stereo"
for i in range(10):
    for pk in vs.encode(av.VideoFrame.from_ndarray(np.full((64, 64, 3), i * 20, np.uint8), format="rgb24")): out.mux(pk)
for pk in vs.encode(None): out.mux(pk)
s = (np.sin(np.arange(44100) / 20) * 3000).astype(np.int16); fr = av.AudioFrame.from_ndarray(np.stack([s, s]), format="s16p", layout="stereo"); fr.sample_rate = 44100
for pk in au.encode(fr): out.mux(pk)
for pk in au.encode(None): out.mux(pk)
out.close()
card = tmp / "cardv"; (card / "scene/blueprints").mkdir(parents=True); bp = card / "scene/blueprints/b.json"
fresh = lambda: bp.write_text(json.dumps({"entryId": "e", "entryType": "actorBlueprint", "title": "x", "actorBlueprint": {"components": {}}}))
fresh(); assert ops.inspect_media(vid, log=print)["audio_unsupported"] is False
real_upload = ct.upload_audio_file
ct.upload_audio_file = lambda p: (_ for _ in ()).throw(RuntimeError("Castle returned HTTP 500:\n<html>boom</html>"))
ops.import_media(bp, card, vid, {}, log=lambda m: None, token="t")
w = ops.take_warnings(); assert len(w) == 1 and "sound wasn't uploaded" in w[0] and "HTTP 500" in w[0], w; assert ops.take_warnings() == []
assert ops.audio_urls(ops.load_blueprint(bp)) == []
ct.upload_audio_file = lambda p: {"url": "http://x/a.mp3"}
fresh(); ops.import_media(bp, card, vid, {}, log=lambda m: None, token="t"); assert ops.take_warnings() == [] and ops.audio_urls(ops.load_blueprint(bp)) == ["http://x/a.mp3"]
media_av.can_encode_mp3 = lambda: False                                                  # a phone build without an MP3 encoder
assert ops.inspect_media(vid, log=print)["audio_unsupported"] is True
fresh(); ops.import_media(bp, card, vid, {}, log=lambda m: None, token="t"); w = ops.take_warnings()
assert w and "MP3" in w[0] and ops.audio_urls(ops.load_blueprint(bp)) == []

# ---- UI: layer list, rename, reorder, delete, import dialog, cauldron deck screen, back button, music note
tasks_q = []
class View:
    can_pop = True; on_confirm_pop = None; popped = []
    async def confirm_pop(self, ok): View.popped.append(ok)
class Page:
    platform = ft.PagePlatform.WINDOWS; height = 800; views = [View()]
    def __init__(self): self.dialogs = []
    def update(self, *a): pass
    def add(self, *c): pass
    def run_task(self, fn, *a): tasks_q.append((fn, a))
    def show_dialog(self, d): self.dialogs.append(d)
    def pop_dialog(self): self.dialogs.pop()
page = Page()
def drain(skip=("tick", "run")):
    while tasks_q:
        fn, a = tasks_q.pop(0)
        if getattr(fn, "__name__", "") in skip: continue
        asyncio.run(fn(*a))
import app as appmod, screens_library as lib, screens_blueprint as sb, cardbar, layers as lyr
import json as _j
app = appmod.App(page); app.api = type("A", (), {"token": "tok", "download_scene_data": lambda self, u: {"snapshot": {"experimentalWeb": {"enabled": True}}}})()
d = app.decks_dir / "deck1"; c = d / "cards/card1"; (c / "scene/blueprints").mkdir(parents=True)
(d / "deck.json").write_text(_j.dumps({"deckId": "deck1", "initialCard": {"cardId": "card1", "sceneDataUrl": "http://s/z"}, "cards": [{"cardId": "card1"}]}))
(c / "card.json").write_text(_j.dumps({"cardId": "card1", "backgroundColor": "#000000", "sceneProperties": {"clock": {"tempo": 100}}}))
(c / "scene/actors.internal.json").write_text("[]")
import _layer_fixtures as TL
bpf = c / "scene/blueprints/hero.json"
bpf.write_text(_j.dumps({"entryId": "h", "title": "Hero", "entryType": "actorBlueprint", "actorBlueprint": {"components": {"Drawing2": TL.base}}}))
ctx = cardbar.CardContext(app, d, "card1")
app.push(lib.home_screen(app)); drain()
view = sb.BlueprintView(app, ctx, bpf); view.tab = "media"; view.render(update=False)
lv = [x for x in view.root.controls[2].content.controls if isinstance(x, ft.ReorderableListView)][0]
titles = lambda: [r.content.controls[1].content.value for r in lv.controls]
assert titles() == ["Layer 3", "Layer 2", "Layer 1"] and lv.show_default_drag_handles is False        # top layer first
assert isinstance(lv.controls[0].content.controls[0], ft.ReorderableDragHandle)
btn = view.root.controls[2].content.controls[1]; assert btn.controls[0].content.controls[-1].value == "New Layer" and btn.alignment == ft.MainAxisAlignment.START
# drag "Layer 1" (display index 2) to the top (display index 0)
view.on_reorder_layers(type("E", (), {"old_index": 2, "new_index": 0})())
assert [l["title"] for l in ops.drawing_layers(ops.load_blueprint(bpf))] == ["Layer 2", "Layer 3", "Layer 1"] and ctx.session.dirty
view.reload(); view.render(update=False)
view.rename_layer_dialog(ops.drawing_layers(view.bp)[0])
dlg = page.dialogs[-1]; field = dlg.content.controls[0]; field.value = "  Ground "; dlg.actions[0].on_click(None)
assert ops.drawing_layers(ops.load_blueprint(bpf))[0]["title"] == "Ground"
view.ask_delete_layer(ops.drawing_layers(view.bp)[1]); page.dialogs[-1].actions[0].on_click(None) if False else None
asyncio.run(page.dialogs[-1].actions[0].on_click(None)); assert len(ops.drawing_layers(ops.load_blueprint(bpf))) == 2
view.reload(); assert ctx.session.undo() and len(ops.drawing_layers(ops.load_blueprint(bpf))) == 3         # undoable
# import dialog for a layer: no "separate actors" choice
gif = tmp / "g.gif"; fr_ = [Image.new("RGB", (16, 16), col) for col in ((255, 0, 0), (0, 255, 0))]; fr_[0].save(gif, save_all=True, append_images=fr_[1:], duration=100, loop=0)
info = ops.inspect_media(gif, log=print)
dd = sb.ImportDialog(view, gif, info, layer="layer2"); dd.options.controls = dd.option_controls(); assert dd.layer == "layer2"
# the deck's top card uses cauldron: message, no cards
app.push(lib.deck_screen(app, d)); host = app.body.content; assert isinstance(host.content, ft.ProgressRing); drain()
assert app.body.content is not host and app.body.content.content.value == "This deck uses cauldron"   # rebuilt, not swapped into the spinner's placeholder
app.pop()
assert ds.cauldron_status(app.cauldron_cache, d) == (True, False)
host2 = lib.deck_screen(app, d).build(app); assert host2.content.value == "This deck uses cauldron"                # now straight from the cache
(d / "deck.json").write_text(_j.dumps({"deckId": "deck1", "initialCard": {"cardId": "card2"}, "cards": [{"cardId": "card1"}]}))
assert not isinstance(lib.deck_screen(app, d).build(app), ft.Container) or lib.deck_screen(app, d).build(app).content is not None
plain = lib.deck_screen(app, d).build(app); assert isinstance(plain, ft.Column)                                   # no sceneDataUrl, not cached: cards shown
# first open after a fresh clone (answer not cached yet): the cards end up in a rebuilt screen, not in the spinner host
(d / "deck.json").write_text(_j.dumps({"deckId": "deck1", "initialCard": {"cardId": "card3", "sceneDataUrl": "http://s/n"}, "cards": [{"cardId": "card1"}]}))
app.api.download_scene_data = lambda u: {"snapshot": {}}
app.push(lib.deck_screen(app, d)); host = app.body.content; assert isinstance(host.content, ft.ProgressRing); drain()
assert app.body.content is not host and isinstance(app.body.content, ft.Column), type(app.body.content); app.pop()
# double tap on a deck: the overlay is up before any network check, and a second tap is ignored
import time as _t
clones = []; seen = []
d9 = app.decks_dir / "deck9"; d9.mkdir(); (d9 / "deck.json").write_text(_j.dumps({"deckId": "deck9", "cards": []}))
def slow_clone(deck_id, task=None): clones.append(deck_id); _t.sleep(0.2); return d9
async def fake_profile(): seen.append(len(page.dialogs)); app.online = True
app.fresh_clone = slow_clone; app.refresh_profile = fake_profile; app.online = False; app.stack[:] = app.stack[:1]
async def taps(): await asyncio.gather(app.open_deck("deck9", None), app.open_deck("deck9", None))
asyncio.run(taps())
assert clones == ["deck9"] and seen == [1], (clones, seen)                # one clone, overlay already shown while checking the network
assert app.opening == set() and len(app.stack) == 2 and not page.dialogs, (app.opening, len(app.stack), page.dialogs)
app.pop()
# back button: Decks screen keeps the system behaviour, everything else is intercepted
app.stack.clear(); app._setup_back(); assert Page.views[0].on_confirm_pop == app._on_confirm_pop
app.push(lib.home_screen(app)); assert Page.views[0].can_pop is True
app.push(lib.deck_screen(app, d)); assert Page.views[0].can_pop is False and len(app.stack) == 2
asyncio.run(app._on_confirm_pop(None)); assert len(app.stack) == 1 and View.popped == [False] and Page.views[0].can_pop is True
asyncio.run(app._on_confirm_pop(None)); assert len(app.stack) == 1                                                  # at Decks: never pops further
# music note
mb = c / "scene/blueprints/tune.json"
import mido
mid = mido.MidiFile(ticks_per_beat=480); tr = mido.MidiTrack(); mid.tracks.append(tr); tr.append(mido.Message("note_on", note=60, velocity=90, time=480)); tr.append(mido.Message("note_off", note=60, velocity=0, time=10))
mp = tmp / "s.mid"; mid.save(mp); mb.write_text(_j.dumps({"entryId": "t", "title": "Tune", "entryType": "actorBlueprint", "actorBlueprint": {"components": {}}})); ops.import_midi(mb, c, mp, log=print)
mv = sb.BlueprintView(app, ctx, mb); mv.tab = "music"; mv.render(update=False)
texts = []
def walk(x):
    if isinstance(x, ft.Text): texts.append(x.value)
    for k in ("controls", "content"):
        v = getattr(x, k, None)
        for y in (v if isinstance(v, list) else [v] if v is not None else []): walk(y)
walk(mv.root); assert "Looping tracks and non-tonal samples aren't implemented" in texts; mv.dispose()
print("changes ok")
