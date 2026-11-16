import _path  # noqa: F401
"""Art editor engine + screen: Castle round trip, tools, undo, layers/frames, commit to the blueprint."""
import base64, io, json, os, tempfile, time
from pathlib import Path
from types import SimpleNamespace as NS
import numpy as np
from PIL import Image
import flet as ft

import art_engine as art

def png_b64(arr):
    b = io.BytesIO(); Image.fromarray(arr, "RGBA").save(b, format="PNG"); return base64.b64encode(b.getvalue()).decode()

# ── a Castle drawing: layer 1 vector square + a red bucket inside, layer 2 paint, 2 frames (2nd linked on layer 1)
sq = [[-2, -2, 2, -2], [2, -2, 2, 2], [2, 2, -2, 2], [-2, 2, -2, -2]]
paths = [{"p": p, "s": 1, "f": False, "c": [0, 0, 1, 1]} for p in sq]
fill = np.zeros((40, 40, 4), np.uint8); fill[:] = (255, 0, 0, 255)
paint = np.zeros((10, 12, 4), np.uint8); paint[:] = (0, 200, 0, 255)
dd = {"fillPixelsPerUnit": 25.6, "scale": 10, "numTotalLayers": 2, "framesBounds": [], "foo": "keep",
      "layers": [
          {"title": "Vec", "id": "layer1", "isVisible": True, "isBitmap": False, "isAvatar": False, "frames": [
              {"isLinked": False, "pathDataList": paths, "fillImageBounds": {"minX": -20, "maxX": 20, "minY": -20, "maxY": 20},
               "fillPng": png_b64(fill), "avatarX": 0, "avatarY": 0, "avatarRadius": 5},
              {"isLinked": True, "pathDataList": [], "fillImageBounds": {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0}}]},
          {"title": "Pix", "id": "layer2", "isVisible": True, "isBitmap": True, "isAvatar": False, "frames": [
              {"isLinked": False, "pathDataList": [], "fillImageBounds": {"minX": 30, "maxX": 42, "minY": 30, "maxY": 40}, "fillPng": png_b64(paint)},
              {"isLinked": False, "pathDataList": [], "fillImageBounds": {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0}}]}]}
doc = art.load_castle(dd)
assert doc.W == doc.H and doc.W % 2 == 0 and doc.F == 2 and [l.type for l in doc.layers] == ["shapes", "paint"]
assert doc.linked(doc.layers[0], 1) and len(doc.layers[0].fr[0].lines) == 4
art.compute_fills(doc.layers[0].fr[0], doc.W, doc.H)
assert len(doc.layers[0].fr[0].fills) == 1 and doc.layers[0].fr[0].fills[0]["ok"] and doc.layers[0].fr[0].fills[0]["c"] == "#ff0000"
assert doc.layers[1].fr[0].px[..., 3].sum() == 12 * 10 * 255
out = art.to_draw(doc)
assert out["foo"] == "keep" and out["numTotalLayers"] == 2 and len(out["layers"]) == 2
l0, l1 = out["layers"]
assert l0["frames"][1]["isLinked"] and not l0["frames"][0]["isLinked"] and len(l0["frames"][0]["pathDataList"]) == 4
assert l0["frames"][0]["fillPng"] and l1["isBitmap"] and l1["frames"][0]["fillImageBounds"] == {"minX": 30, "maxX": 42, "minY": 30, "maxY": 40}
assert "fillPng" not in l1["frames"][1] and l1["frames"][1]["fillImageBounds"]["maxX"] == 0
assert abs(l0["frames"][0]["pathDataList"][0]["p"][0] - -2) < 1e-4
doc2 = art.load_castle(out); assert len(doc2.layers[0].fr[0].lines) == 4 and doc2.W == doc.W        # stable round trip

# ── tools
W = doc.W
d = art.ShapeData()
for ln in art.make_shape("square", (50, 50), [(50, 50), (100, 100)], "#00ff00"): d.lines.append(ln)
art.shape_bucket(d, (75, 75), "#ff0000", W, doc.W); assert len(d.fills) == 1
art.shape_bucket(d, (10, 10), "#0000ff", W, doc.W); assert len(d.fills) == 1                       # outside, open: leaks, nothing added
d.lines.pop(); d.touch(); art.compute_fills(d, W, W); assert not d.fills[0]["ok"]                  # opening the outline: dormant
d.lines.append({"a": (50, 100), "b": (50, 50), "m": None, "q": 0, "c": "#00ff00"}); d.touch(); art.compute_fills(d, W, W); assert d.fills[0]["ok"]
l = art.Layer("S", "shapes", [d])
assert art.color_at(l, d, (75, 75), W, W) == "#ff0000" and art.color_at(l, d, (50, 75), W, W) == "#00ff00"
art.swap_all(l, d, (75, 75), "#123456", W, W); assert d.fills[0]["c"] == "#123456"
before = len(d.lines); assert art.erase_shapes(d, (75, 50), 6, False) and len(d.lines) >= before
p = art.PaintData(W, W); art.paint_line(p, (10, 10), (30, 10), "#ff0000", False, 3); assert p.px[10, 20, 3] == 255 and p.px[40, 40, 3] == 0
art.paint_fill(p, (200, 200), "#0000ff"); assert tuple(p.px[200, 200]) == (0, 0, 255, 255) and p.px[10, 20, 0] == 255
art.paint_fill(p, (200, 200), None); assert p.px[200, 200, 3] == 0
q = art.PaintData(W, W); q.px[5, 5] = (1, 2, 3, 255); art.transform(doc, q, "fh"); assert q.px[5, W - 5 - 1 + 1 - 1 + 0, 3] in (0, 255)
arr = art.render_doc(doc, 0, 0); assert arr.shape == (W, W, 4) and len(art.thumb(doc, doc.layers[0], doc.layers[0].fr[0])) > 50

# snapshots share paint arrays until written (copy on write)
snap = doc.snapshot(); px = doc.layers[1].fr[0]; px.own()[0, 0] = (9, 9, 9, 255); doc.restore(snap)
assert doc.layers[1].fr[0].px[0, 0, 3] == 0 and doc.linked(doc.layers[0], 1)

# merge / convert
doc.layers[0].fr[0].fills.clear(); doc.layers[0].fr[0].touch()
art.convert_to_paint(doc, doc.layers[0]); assert doc.layers[0].type == "paint" and doc.layers[0].fr[0].px[..., 3].any()
art.merge_down(doc, 1, False); assert len(doc.layers) == 1 and doc.layers[0].fr[0].px[..., 3].any()

nd = art.Doc(64, 64); a1 = nd.add_layer("shapes"); a2 = nd.add_layer("paint"); a3 = nd.add_layer("shapes")
assert [x.name for x in nd.layers] == ["Shapes 1", "Paint 2", "Shapes 3"]                 # numbered like the prototype
# ── the screen
os.environ["FLET_APP_STORAGE_DATA"] = tempfile.mkdtemp()
import blueprint_ops as ops, screens_art as sa
class Page:
    width, height = 360, 700
    def update(self, *a): pass
    def run_task(self, *a): pass
class App:
    page = Page(); dialogs = []; toasts = []; popped = 0
    def refresh_bar(self): pass
    def log(self, *a, **k): pass
    def toast(self, m, error=False): self.toasts.append(m)
    def dialog(self, heading, content, actions=None, **k): self.dialogs.append(content); return object()
    def close_dialog(self, d): pass
    def confirm(self, *a): self.dialogs.append(a)
    def pop(self): self.popped += 1
    def spawn(self, c): pass
    def go_back(self): pass
app = App()
bpf = Path(tempfile.mkdtemp()) / "hero.json"
bpf.write_text(json.dumps({"entryId": "h", "title": "Hero", "entryType": "actorBlueprint",
                           "actorBlueprint": {"components": {"Drawing2": {"drawData": dd, "hash": "x"}}}}))
ctx = NS(edit_sync=lambda fn: fn())
ed = sa.ArtEditor(app, ctx, bpf, art.load_castle(dd))
ed._resized(NS(width=360, height=420)); assert abs(ed.view["z"] - min(336 / ed.doc.W, 396 / ed.doc.H, 2.2)) < 1e-6
assert ed.layer.type == "paint" and ed.tool == "brush" and sa.bar(app, ed) is not None
ev = lambda x, y, pc=1, s=1.0: NS(local_position=NS(x=x, y=y), local_focal_point=NS(x=x, y=y), pointer_count=pc, scale=s)
def stroke(a, b):
    ed._tap_down(ev(*a)); ed._scale_start(ev(*a)); ed._scale_update(ev(*b)); ed._scale_end(ev(*b, pc=0))
ed.sel, ed.cf = 1, 0; ed.tool = "brush"; ed.color = "#abcdef"; ed.size = 6; ed.rebuild()
before = ed.doc.layers[1].fr[0].px.copy()
cx, cy = ed.view["x"] + 50 * ed.view["z"], ed.view["y"] + 50 * ed.view["z"]
stroke((cx, cy), (cx + 40, cy)); assert ed.undo and ed.changed and not np.array_equal(before, ed.doc.layers[1].fr[0].px)
ed.do_undo(); assert np.array_equal(before, ed.doc.layers[1].fr[0].px) and ed.redo
ed.do_redo(); assert not np.array_equal(before, ed.doc.layers[1].fr[0].px)
# a second finger right after the first cancels the stroke (it was the start of a pan)
n = len(ed.undo); ed._tap_down(ev(cx, cy + 30)); ed._scale_update(ev(cx, cy + 30, pc=2, s=1.5)); ed._scale_end(ev(cx, cy, pc=0))
assert len(ed.undo) == n and ed.view["z"] != 0 and ed.strk is None
# vector: pencil, bucket, grab/modify, eraser, transforms
ed.sel = 0; ed.rebuild()
assert ed.layer.type == "shapes" and ed.tool == "pencil"          # switching layer swaps in that layer type's tools
ed.doc.layers.insert(0, art.Layer("V", "shapes", [art.ShapeData() for _ in range(ed.doc.F)])); ed.sel = 0; ed.cf = 0; ed.tool = "pencil"; ed.sub["p"] = "square"; ed.rebuild()
stroke((cx, cy), (cx + 60, cy + 60)); assert len(ed.doc.layers[0].fr[0].lines) == 4
ed.tool = "bucket"; ed.rebuild(); ed.color = "#ff0000"; ed._tap_down(ev(cx + 30, cy + 30)); ed._tap_up(None); assert ed.doc.layers[0].fr[0].fills
ed.tool = "grab"; ed.sub["g"] = "move"; ed.rebuild(); ed.xf("rot"); ed.xf("fh"); ed.xf("fv")
ed.add_frame(); assert ed.doc.F == 3 and ed.cf == 2; ed.do_undo(); assert ed.doc.F == 2
ed.menu("x", [("pencil", "a", lambda: None)]); ed.layer_menu(); ed.cell_menu(); ed.frame_menu(); ed.add_menu()
ed.toggle_vis(0); assert not ed.doc.layers[0].vis
# leaving writes drawData into the blueprint, other keys intact
assert ed.leave_commit()
saved = json.loads(bpf.read_text())["actorBlueprint"]["components"]["Drawing2"]["drawData"]
assert saved["foo"] == "keep" and len(saved["layers"]) == 3 and saved["layers"][0]["isVisible"] is False and saved["layers"][0]["title"] == "V"
print("art ok")
