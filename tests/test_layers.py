import _path  # noqa: F401
import base64, io, json, tempfile
from pathlib import Path
from PIL import Image
import blueprint_ops as ops
import castletool_core as ct
import layers as lyr


def png(w, h, color=(255, 0, 0, 255)):
    buf = io.BytesIO(); Image.new("RGBA", (w, h), color).save(buf, format="PNG"); return buf.getvalue()


def frame(vec=False, bitmap=None):
    fr = {"isLinked": False, "pathDataList": [], "fillImageBounds": {"minX": 0, "maxX": 0, "minY": 0, "maxY": 0},
          "avatarX": 0, "avatarY": 0, "avatarRadius": 5}
    if vec:
        fr["pathDataList"] = [{"p": [-3, -4, 6, 7], "s": 1, "f": False, "c": [1, 0, 0, 1]}]
    if bitmap:
        w, h = bitmap
        fr["fillPng"] = base64.b64encode(png(w, h)).decode()
        fr["fillImageBounds"] = {"minX": -w / 2, "maxX": w / 2, "minY": -h / 2, "maxY": h / 2}
    return fr


def layer(i, bitmap=False, n=2, size=(64, 64)):
    return {"title": f"Layer {i}", "id": f"layer{i}", "isVisible": True, "isBitmap": bitmap, "isAvatar": False,
            "frames": [frame(vec=not bitmap, bitmap=size if bitmap else None) for _ in range(n)]}


def drawing(layers, ppu=25.6, scale=10, n=2):
    return {"initialFrame": 1, "currentFrame": 1, "framesPerSecond": 4, "playMode": "still", "loopStartFrame": -1,
            "loopEndFrame": -1, "opacity": 1, "hash": "1", "playing": False, "loop": False,
            "drawData": {"color": [1, 1, 1, 1], "lineColor": [0, 0, 0, 1], "gridSize": 0.7, "scale": scale, "version": 3,
                         "fillPixelsPerUnit": ppu, "numTotalLayers": len(layers),
                         "framesBounds": [dict(lyr.DEFAULT_BOUNDS) for _ in range(n)], "colors": [],
                         "selectedFrame": 1, "layers": layers},
            "physicsBodyData": {"shapes": [], "scale": scale, "version": 2, "zeroShapesInV1": False}, "disabled": False}


def imported(bitmap, n=1, ppu=None, size=(32, 32)):
    ppu = ppu or (3 if bitmap else 25.6)
    d = drawing([layer(1, bitmap, n, size)], ppu=ppu, n=n)
    d["framesPerSecond"] = 12; d["playMode"] = "loop"; d["loop"] = True
    return d


ids = lambda d: [l["id"] for l in lyr.layers_of(d)]

# ---- summary / counts
base = drawing([layer(1), layer(2), layer(3, True)])
assert [s["bitmap"] for s in lyr.summary(base)] == [False, False, True] and lyr.frame_count(base) == 2

# ---- overwrite a vector layer with a bitmap file: it becomes a bitmap, others untouched, density matched
out = lyr.apply_drawing(base, imported(True), "layer2")
l2 = lyr.layers_of(out)[1]
assert ids(out) == ["layer1", "layer2", "layer3"] and l2["isBitmap"] and l2["title"] == "Layer 2"
assert lyr.layers_of(out)[0] == lyr.layers_of(base)[0] and lyr.layers_of(out)[2] == lyr.layers_of(base)[2]
assert len(l2["frames"]) == 2 and "fillPng" in l2["frames"][0] and "fillPng" not in l2["frames"][1]   # padded with a blank
b = l2["frames"][0]["fillImageBounds"]; w = b["maxX"] - b["minX"]
assert abs(w - 32 * 25.6 / 3) < 1.5, w                                      # 32px @3ppu keeps its Castle-unit size at 25.6ppu
img = Image.open(io.BytesIO(base64.b64decode(l2["frames"][0]["fillPng"]))); assert abs(img.size[0] - w) < 1.01
assert out["hash"] != base["hash"] and base["drawData"]["layers"][1]["isBitmap"] is False      # input not mutated

# ---- overwrite a bitmap layer with a vector file: becomes vector, old bitmap gone
vec = lyr.apply_drawing(base, imported(False), "layer3")
l3 = lyr.layers_of(vec)[2]
assert not l3["isBitmap"] and all("fillPng" not in f for f in l3["frames"]) and l3["frames"][0]["pathDataList"]

# ---- more frames than the drawing has: other layers get empty frames, none of their content changes
longer = lyr.apply_drawing(base, imported(True, n=5, ppu=25.6), "layer1")
assert lyr.frame_count(longer) == 5 and all(len(l["frames"]) == 5 for l in lyr.layers_of(longer))
assert lyr.layers_of(longer)[1]["frames"][:2] == lyr.layers_of(base)[1]["frames"]
assert all(not f["pathDataList"] and "fillPng" not in f for f in lyr.layers_of(longer)[1]["frames"][2:])
assert len(longer["drawData"]["framesBounds"]) == 5
assert longer["framesPerSecond"] == 4                                        # drawing already animated (2 frames): keep its settings
still = drawing([layer(1, n=1), layer(2, n=1)], n=1)
anim = lyr.apply_drawing(still, imported(True, n=4, ppu=25.6), lyr.NEW)
assert anim["framesPerSecond"] == 12 and anim["playMode"] == "loop"          # a still became an animation

# ---- new layer: on top, unique id/title, numTotalLayers grows
nl = lyr.apply_drawing(base, imported(False), lyr.NEW)
assert ids(nl) == ["layer1", "layer2", "layer3", "layer4"] and lyr.layers_of(nl)[3]["title"] == "Layer 4"
assert nl["drawData"]["numTotalLayers"] == 4 and not lyr.layers_of(nl)[3]["isBitmap"]
assert ids(lyr.apply_drawing(nl, imported(True), lyr.NEW))[-1] == "layer5"

# ---- the only layer: the import becomes the whole drawing but keeps the layer's identity
solo = drawing([{**layer(1), "title": "Mine", "id": "layerX"}])
r = lyr.apply_drawing(solo, imported(True, ppu=3), "layerX")
assert r["drawData"]["fillPixelsPerUnit"] == 3 and lyr.layers_of(r)[0]["title"] == "Mine" and ids(r) == ["layerX"]

# ---- nothing there yet / plain import
assert lyr.apply_drawing(None, imported(True), lyr.NEW)["drawData"]["layers"][0]["id"] == "layer1"
assert lyr.apply_drawing(base, imported(True), None) == imported(True)
try:
    lyr.apply_drawing(base, imported(True), "nope"); raise SystemExit("missing layer accepted")
except lyr.LayerError:
    pass

# ---- rename / delete / move
d = json.loads(json.dumps(base))
lyr.rename_layer(d, "layer2", "  Water "); assert lyr.layers_of(d)[1]["title"] == "Water"
for bad in ("", "   "):
    try: lyr.rename_layer(d, "layer2", bad); raise SystemExit("empty name accepted")
    except lyr.LayerError: pass
lyr.move_layer(d, 0, 2); assert ids(d) == ["layer2", "layer3", "layer1"]
lyr.delete_layer(d, "layer3"); assert ids(d) == ["layer2", "layer1"]
lyr.delete_layer(d, "layer2")
try: lyr.delete_layer(d, "layer1"); raise SystemExit("deleted the last layer")
except lyr.LayerError: pass

# ---- through blueprint_ops, on disk, and a real image import into a layer
t = Path(tempfile.mkdtemp()); card = t / "card"; (card / "scene" / "blueprints").mkdir(parents=True)
bp = card / "scene" / "blueprints" / "b.json"
bp.write_text(json.dumps({"entryId": "e1", "entryType": "actorBlueprint", "title": "x",
                          "actorBlueprint": {"components": {"Drawing2": base}}}))
ops.rename_layer(bp, "layer1", "Sky"); ops.move_layer(bp, 0, 2); ops.delete_layer(bp, "layer3")
assert [l["title"] for l in ops.drawing_layers(ops.load_blueprint(bp))] == ["Layer 2", "Sky"]
try: ops.delete_layer(bp, "layer2") or ops.delete_layer(bp, "layer1"); raise SystemExit("last layer deleted")
except ops.OpError: pass
bp.write_text(json.dumps({"entryId": "e1", "entryType": "actorBlueprint", "title": "x",
                          "actorBlueprint": {"components": {"Drawing2": base}}}))
img = t / "pic.png"; Image.new("RGBA", (40, 20), (0, 255, 0, 255)).save(img)
ops.import_media(bp, card, img, {}, log=lambda m: None, token="x", layer="layer2")
got = ops.drawing_layers(ops.load_blueprint(bp)); assert [(l["id"], l["bitmap"]) for l in got] == [("layer1", False), ("layer2", True), ("layer3", True)], got
ops.import_media(bp, card, img, {}, log=lambda m: None, token="x", layer=lyr.NEW)
got = ops.drawing_layers(ops.load_blueprint(bp)); assert [l["id"] for l in got][-1] == "layer4" and got[-1]["bitmap"]
# several images → frames of the ONE layer, never separate actors
img2 = t / "pic2.png"; Image.new("RGBA", (40, 20), (0, 0, 255, 255)).save(img2)
import zipfile
z = t / "two.zip"
with zipfile.ZipFile(z, "w") as zf: zf.write(img, "a.png"); zf.write(img2, "b.png")
before = sorted(p.name for p in (card / "scene" / "blueprints").iterdir())
ops.import_media(bp, card, z, {}, log=lambda m: None, token="x", layer="layer1")
assert sorted(p.name for p in (card / "scene" / "blueprints").iterdir()) == before
d2 = ops.components(ops.load_blueprint(bp))["Drawing2"]; assert len(lyr.layers_of(d2)[0]["frames"]) == 2 and lyr.layers_of(d2)[0]["isBitmap"]
print("layers ok")
