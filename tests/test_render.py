import _path  # noqa: F401  (puts src/ on sys.path)
"""Regression: a Castle-made vector+fill blueprint must render like Castle's own thumbnail
(Y-down vectors, bend points, fill under lines)."""
import base64, io, json
import numpy as np
from PIL import Image
import drawing

bp = json.load(open(str(_path.FIXTURES / "cursor.json")))
thumb = np.asarray(Image.open(io.BytesIO(base64.b64decode(bp["base64Png"]))).convert("RGBA"))[..., 3] > 0

def crop(m):
    ys, xs = np.where(m)
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

def iou(png):
    mine = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3] > 40
    a = np.asarray(Image.fromarray((crop(thumb) * 255).astype("uint8")).resize((128, 128))) > 127
    b = np.asarray(Image.fromarray((crop(mine) * 255).astype("uint8")).resize((128, 128))) > 127
    return (a & b).sum() / (a | b).sum()

good = iou(drawing.render_png(bp, 256, None))
assert good > 0.93, good
# the old (flipped) behaviour would be far off
d2 = bp["actorBlueprint"]["components"]["Drawing2"]
import copy
f = copy.deepcopy(bp)
for l in f["actorBlueprint"]["components"]["Drawing2"]["drawData"]["layers"]:
    for fr in l["frames"]:
        for s in fr["pathDataList"]:
            s["p"][1] *= -1; s["p"][3] *= -1
            if "bp" in s: s["bp"]["y"] *= -1
assert iou(drawing.render_png(f, 256, None)) < good - 0.2
print("render ok", round(good, 3))

# arcs: bend points are circles through (p1, bp, p2) - tested against Castle's thumbnail
import copy, math
from scipy.ndimage import binary_dilation
d2 = bp["actorBlueprint"]["components"]["Drawing2"]
full = np.asarray(Image.open(io.BytesIO(drawing.render_png(bp, 512, None))).convert("RGBA"))[..., 3] > 40
def bbox(m):
    ys, xs = np.where(m); return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
bx = bbox(full)
nofill = copy.deepcopy(d2)
for l in nofill["drawData"]["layers"]:
    for fr in l["frames"]: fr.pop("fillPng", None)
vec = np.asarray(drawing.render_frame(nofill, 0, 512, None, bounds=drawing._scene_bounds(d2, [0])))[..., 3] > 100
T = np.asarray(Image.open(io.BytesIO(base64.b64decode(bp["base64Png"]))).convert("RGBA")).astype(int)
white = (T[..., 0] > 200) & (T[..., 1] > 200) & (T[..., 2] > 200) & (T[..., 3] > 0)
tb = bbox(T[..., 3] > 0)
W = Image.fromarray((white[tb[1]:tb[3], tb[0]:tb[2]] * 255).astype("uint8")).resize((bx[2] - bx[0], bx[3] - bx[1]), Image.BILINEAR)
wm = np.zeros((512, 512), bool); wm[bx[1]:bx[3], bx[0]:bx[2]] = np.asarray(W) > 100
cover = (vec & binary_dilation(wm, iterations=3)).sum() / vec.sum()
assert cover > 0.97, cover
print("arc coverage ok", round(cover, 3))
