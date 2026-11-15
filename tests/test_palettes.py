"""Palette snapping + original-quality mode."""
import io

import _path  # noqa: F401
import numpy as np
from PIL import Image

import blueprint_ops as ops
import castletool_core as ct
import palettes as pal


def png(arr):
    b = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(b, format="PNG")
    return b.getvalue()


def decode(b):
    return np.array(Image.open(io.BytesIO(b)).convert("RGBA"))


def main():
    assert len(pal.PALETTES) == 15 and pal.PALETTES["AAP-64"][0] == (6, 6, 8)
    assert {k: len(v) for k, v in pal.PALETTES.items()}["DB-32"] == 32
    # nearest RGB, alpha kept, union of palettes
    a = np.zeros((1, 3, 4), np.uint8)
    a[0, 0] = (250, 250, 250, 255)   # -> white
    a[0, 1] = (10, 10, 10, 128)      # -> near black, alpha 128
    a[0, 2] = (0, 0, 0, 0)           # transparent stays transparent
    out = decode(pal.apply_palettes(png(a), ["SOBEACHY-8"]))
    assert tuple(out[0, 0, :3]) in pal.PALETTES["SOBEACHY-8"] and out[0, 1, 3] == 128 and out[0, 2, 3] == 0
    both = decode(pal.apply_palettes(png(a), ["SOBEACHY-8", "DB-32"]))
    assert tuple(both[0, 0, :3]) == (255, 255, 255) and tuple(both[0, 1, :3]) == (0, 0, 0)
    assert pal.apply_palettes(png(a), []) == png(a)
    # quantize first, then palette
    rng = np.random.default_rng(1)
    big = np.dstack([rng.integers(0, 256, (16, 16, 3), dtype=np.uint8), np.full((16, 16, 1), 255, np.uint8)])
    red = ct.reduce_frames([png(big)], {"quantize": 4, "palettes": ["MUSHROOM"]})[0]
    colors = {tuple(c) for c in decode(red).reshape(-1, 4)[:, :3]}
    assert colors <= set(pal.PALETTES["MUSHROOM"]) and len(colors) <= 4
    # original quality ignores quantize / palettes / smooth for pictures
    o = dict(ops.DEFAULT_MEDIA_OPTS, fixed_px=True, pixel_size=0.2, quantize=8, palettes=["DB-32"], smooth=True)
    st = ops._media_state(o, "image", [], {}, False)
    assert st["quantize"] == 0 and st["palettes"] == [] and not st["smooth"] and st["fixed_scale"] == 1 / 0.2
    o["fixed_px"] = False
    st = ops._media_state(o, "image", [], {}, False)
    assert st["quantize"] == 8 and st["palettes"] == ["DB-32"]
    print("palettes ok")


if __name__ == "__main__":
    main()
