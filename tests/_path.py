"""Test helpers: put src/ on sys.path and build fixtures without system fonts or ffmpeg."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SRC))


def make_test_font(path: Path) -> Path:
    """A tiny TrueType font (digits + A-Z + a-z, asymmetric glyphs) built with fontTools."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    names = [".notdef", "space"] + [f"g{ord(c)}" for c in chars]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(names)
    fb.setupCharacterMap({32: "space", **{ord(c): f"g{ord(c)}" for c in chars}})

    def glyph(shape: str):
        pen = TTGlyphPen(None)
        if shape == "L":      # tall stem + foot: clearly not vertically symmetric
            pts = [(100, 0), (600, 0), (600, 150), (250, 150), (250, 700), (100, 700)]
        else:
            pts = [(50, 0), (550, 0), (550, 500), (50, 500)]
        pen.moveTo(pts[0])
        for p in pts[1:]:
            pen.lineTo(p)
        pen.closePath()
        return pen.glyph()

    glyphs = {".notdef": glyph("box"), "space": TTGlyphPen(None).glyph()}
    for c in chars:
        glyphs[f"g{ord(c)}"] = glyph("L")
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (650, 0) for n in names})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "TestFont", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    fb.setupPost()
    fb.save(str(path))
    return path


def make_test_video(path: Path, frames: int = 10, size=(64, 48), fps: int = 10, with_audio: bool = False) -> Path:
    """A short mp4 made with PyAV (no ffmpeg binary needed)."""
    import av
    import numpy as np
    out = av.open(str(path), "w")
    st = out.add_stream("mpeg4", rate=fps)
    st.width, st.height, st.pix_fmt = size[0], size[1], "yuv420p"
    for i in range(frames):
        arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        arr[:, :, 0] = (i * 25) % 255
        arr[:, : size[0] // 2, 1] = 200
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in st.encode(frame):
            out.mux(pkt)
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()
    return path
