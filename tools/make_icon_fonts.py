#!/usr/bin/env python3
"""Build the two small Material Symbols fonts the app uses.

Input : the official variable font  MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf
Output: assets/fonts/MaterialSymbols-W200.ttf         (weight 200, unfilled)
        assets/fonts/MaterialSymbols-W200-Filled.ttf  (weight 200, filled; used for lock)
Both are subset to only the icons listed in theme.MD, so each is a few KB.

    python tools/make_icon_fonts.py "path/to/MaterialSymbolsOutlined[FILL,GRAD,opsz,wght].ttf"
"""
import sys
from pathlib import Path

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from theme import MD  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def build(src: Path, fill: int, out: Path) -> None:
    font = TTFont(src)
    opts = Options()
    opts.layout_features = []          # codepoints only; no ligature tables needed
    opts.glyph_names = False
    opts.notdef_outline = True
    sub = Subsetter(opts)
    sub.populate(unicodes=list(MD.values()))
    sub.subset(font)                   # subset first (keeps fvar/gvar): instancing is then fast
    static = instancer.instantiateVariableFont(
        font, {"FILL": fill, "GRAD": 0, "opsz": 24, "wght": 200}, inplace=False
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    static.save(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    if not src.exists():
        sys.exit(f"not found: {src}")
    build(src, 0, OUT / "MaterialSymbols-W200.ttf")
    build(src, 1, OUT / "MaterialSymbols-W200-Filled.ttf")


if __name__ == "__main__":
    main()
