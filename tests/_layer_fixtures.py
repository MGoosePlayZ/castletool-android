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


base = drawing([layer(1), layer(2), layer(3, True)])
