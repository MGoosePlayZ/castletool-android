import _path  # noqa: F401
"""0.7.0: scene-data 403 is skipped, skipped animation frames aren't processed, MIDI volume offset,
several picked files staged as one import."""
import io, json, os, tempfile
from pathlib import Path
from PIL import Image

tmp = Path(tempfile.mkdtemp()); os.environ["FLET_APP_STORAGE_DATA"] = str(tmp)

# ---- 403 on scene data: the clone goes on without that card, the cauldron check isn't repeated
import castle_api as ca, deck_store as ds
class R403:
    status_code = 403
    def raise_for_status(self): raise AssertionError("403 must be handled before raise_for_status")
orig_get = ca.requests.get
ca.requests.get = lambda *a, **k: R403()
try:
    ca.CastleAPI.download_scene_data(object.__new__(ca.CastleAPI), "https://scenedata.castlexyz.com/?cardId=c1")
    raise SystemExit("403 not raised")
except ca.SceneDataForbidden: pass
ca.requests.get = orig_get

class Api403:
    def deck(self, deck_id):
        card = {"cardId": "c1", "title": "x", "sceneDataUrl": "https://scenedata.castlexyz.com/?cardId=c1&version=1"}
        return {"deckId": deck_id, "title": "T", "initialCard": card, "cards": [card]}
    def download_scene_data(self, url): raise ca.SceneDataForbidden("403")
logs = []
d = ds.clone_deck(Api403(), "deck403", tmp / "decks", logs.append)
assert (d / "deck.json").exists() and not (d / "cards" / "c1").exists()
assert any("403" in m for m in logs)
cache = tmp / "cauldron.json"
assert ds.detect_cauldron(Api403(), d, cache) is False                     # no exception: the deck just opens
assert json.loads(cache.read_text())["c1"]["unreadable"] is True
assert ds.cauldron_status(cache, d) == (False, False)                      # answered: not fetched again

# ---- every Nth frame: the skipped frames are never resized or encoded
import castletool_core as ct
gif = tmp / "a.gif"
frames = [Image.new("RGB", (8, 8), (i * 12, 0, 0)) for i in range(20)]
frames[0].save(gif, save_all=True, append_images=frames[1:], duration=100, loop=0)
seen = {"n": 0}
orig_resize = Image.Image.resize
def counting(self, *a, **k):
    seen["n"] += 1
    return orig_resize(self, *a, **k)
Image.Image.resize = counting
got, fps = ct.load_image_frames(gif, 16, 16, every_n=2)
Image.Image.resize = orig_resize
assert len(got) == 10 and seen["n"] == 10, (len(got), seen)
assert abs(fps - 5.0) < 0.01, fps                                         # 10 fps / 2
got1, fps1 = ct.load_image_frames(gif, 16, 16)
assert len(got1) == 20 and abs(fps1 - 10.0) < 0.01

# ---- MIDI volume offset: clamped, tone notes only
import mido
assert ct.clamp_volume_offset(500) == 127 and ct.clamp_volume_offset(-500) == -127
assert ct.clamp_volume_offset("x") == 0 and ct.clamp_volume_offset(3.6) == 4
mid = mido.MidiFile(ticks_per_beat=480); tr = mido.MidiTrack(); mid.tracks.append(tr)
tr.append(mido.Message("note_on", note=60, velocity=100, time=0))
tr.append(mido.Message("note_off", note=60, velocity=0, time=480))
def vels(m): return sorted(n["vel"] for t in m["song"]["tracks"] for p in t["sequence"].values()
                           for n in m["song"]["patterns"][p["patternId"]]["notes"].values() for n in n)
assert vels(ct.build_music(mid)) == [100]
assert vels(ct.build_music(mid, volume_offset=20)) == [120]
assert vels(ct.build_music(mid, volume_offset=100)) == [127]
assert vels(ct.build_music(mid, volume_offset=-50)) == [50]
assert vels(ct.build_music(mid, volume_offset=-999)) == [1]

# ---- several picked files become one import (a folder is read like an archive)
import blueprint_ops as ops
root = tmp / "stage"
for i in range(3):
    (root / f"{i + 1:04d}").mkdir(parents=True)
    Image.new("RGBA", (4 + i, 4), (255, 0, 0, 255)).save(root / f"{i + 1:04d}" / f"pic{i}.png")
info = ops.inspect_media(root, log=print)
assert info["category"] == "image" and info["count"] == 3, info
assert info["names"] == ["pic0.png", "pic1.png", "pic2.png"], info["names"]
print("v0.7 ok")
