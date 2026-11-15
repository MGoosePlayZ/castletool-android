"""Fixed color palettes for the image importer.

Pick one or more palettes: every pixel is snapped to the nearest color (plain RGB
distance) of the UNION of the picked palettes. Runs after the optional basic
quantization, so "reduce to N colors" first, then "snap to the palette".
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

_RAW = {
    "AAP-64": "060608 141013 3b1725 73172d b4202a df3e23 fa6a0a f9a31b ffd541 fffc40 d6f264 9cdb43 59c135 14a02e 1a7a3e 24523b 122020 143464 285cc4 249fde 20d6c7 a6fcdb ffffff fef3c0 fad6b8 f5a097 e86a73 bc4a9b 793a80 403353 242234 221c1a 322b28 71413b bb7547 dba463 f4d29c dae0ea b3b9d1 8b93af 6d758d 4a5462 333941 422433 5b3138 8e5252 ba756a e9b5a3 e3e6ff b9bffb 849be4 588dbe 477d85 23674e 328464 5daf8d 92dcba cdf7e2 e4d2aa c7b08b a08662 796755 5a4e44 423934",
    "DELPHINIUM-17": "645191 8378ad c3b2d0 e6e5ff 72396c b4548f e0aacd c9365e e8556f f9abb4 ea8e65 f5cfa7 64584a 77763e cac16b e5dd9c f5f0ea",
    "CHOCOMILK-8": "d6f5e4 f7ffc5 b4c788 8a8969 8b6a44 62554c 463c3c 312629",
    "SOBEACHY-8": "e55388 e57d88 e59f88 e5d988 e3d5cc bad5cc 6dd5cc 5ac5cc",
    "DEEP-MAZE": "f2ff66 9af089 38d88e 00be91 009a98 085562 001d2a",
    "GRAPE-SODA": "aa81ff b837ea aa11bb 90006c 56002a 25000d 000000 15021e 47054f c9298c ee699e ffbac0 fff6f6",
    "FANTASY-24": "1f240a 39571c a58c27 efac28 efd8a1 ab5c1c 183f39 ef692f efb775 a56243 773421 724113 2a1d0d 392a1c 684c3c 927e6a 276468 ef3a0c 45230d 3c9f9c 9b1a0a 36170c 550f0a 300f0a",
    "CHASM": "85daeb 5fc9e7 5fa1e7 5f6ee7 4c60aa 444774 32313b 463c5e 5d4776 855395 ab58a8 ca60ae f3a787 f5daa7 8dd894 5dc190 4ab9a3 4593a5 5efdf7 ff5dcc fdfe89 ffffff",
    "DB-32": "000000 222034 45283c 663931 8f563b df7126 d9a066 eec39a fbf236 99e550 6abe30 37946e 4b692f 524b24 323c39 3f3f74 306082 5b6ee1 639bff 5fcde4 cbdbfc ffffff 9badb7 847e87 696a6a 595652 76428a ac3232 d95763 d77bba 8f974a 8a6f30",
    "MUSHROOM": "2e222f 45293f 7a3045 993d41 cd683d fbb954 f2ec8b b0a987 997f73 665964 443846 576069 788a87 a9b2a2",
    "INDECISION": "fff4e0 8fcccb 449489 285763 2f2b5c 4b3b9c 457cd6 f2b63d d46e33 e34262 94353d 57253b 9c656c d1b48c b4ba47 6d8c32 2c1b2e",
    "DEVILFOOD": "160d10 503146 6a3c58 934f65 7f3265 6a2249 912749 a6333c b54846 bf5e4d a13f2f 7b2c32 58232f 351d32 3e2544 4a3360 674268 b97884 4b5e7d 34377e 2b2948 1e1c32 221425 471f2f 652c39 773a40 8e524c 5d4143 3a363c 37272a 4b3233",
    "LOGCABIN": "73383e ad5845 cf784c e8ad80 f5e5bf f7ffed b5f5ec 5bace3 636ab8 574175 2f2338",
    "WISTERIC": "c9dab8 86c1a3 569082 2d535b 162841 100b27 331541 732d72 a75b80 cd7a87 f3b5a8 ffddcb e6c9c3 cea3ad b37fa1 8d5489 53315a",
    "INTERSTATE-28": "fefedf f4eaa3 f4d565 edb048 bb5e25 903a25 6f1519 a11920 da453c f2846c e8676a c14166 942a5e 631649 23192d 342e4c 465374 5a8090 78ccb9 57a386 3a716c 27424b 1f2a2c 474214 55632e 89954c a1b974 b6e3b3",
}


def _parse(s: str) -> list[tuple[int, int, int]]:
    return [(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)) for h in s.split()]


PALETTES: dict[str, list[tuple[int, int, int]]] = {k: _parse(v) for k, v in _RAW.items()}
PALETTE_NAMES: list[str] = list(PALETTES)


def union_colors(names) -> np.ndarray:
    """Unique RGB colors of all the named palettes, as an (N, 3) int array."""
    seen: dict[tuple[int, int, int], None] = {}
    for n in names:
        for c in PALETTES.get(n, ()):
            seen[c] = None
    return np.array(list(seen), dtype=np.int32).reshape(-1, 3)


def apply_palettes_rgba(arr: np.ndarray, names) -> np.ndarray:
    """(H, W, 4) uint8 -> same shape, RGB snapped to the nearest union color (alpha kept)."""
    pal = union_colors(names)
    if not len(pal):
        return arr
    rgb = arr[..., :3].reshape(-1, 3)
    uniq, inverse = np.unique(rgb, axis=0, return_inverse=True)        # distances only for distinct colors
    d = ((uniq[:, None, :].astype(np.int32) - pal[None, :, :]) ** 2).sum(axis=2)
    mapped = pal[d.argmin(axis=1)].astype(np.uint8)
    out = arr.copy()
    out[..., :3] = mapped[inverse.reshape(-1)].reshape(arr.shape[0], arr.shape[1], 3)
    return out


def apply_palettes(png_bytes: bytes, names) -> bytes:
    """PNG in -> PNG out with every pixel snapped to the union of the named palettes."""
    names = [n for n in (names or []) if n in PALETTES]
    if not names:
        return png_bytes
    img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    out = Image.fromarray(apply_palettes_rgba(np.array(img), names), "RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
