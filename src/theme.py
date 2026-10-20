"""Castle look: monochrome palette, fonts, and icon helpers.

Fonts live in ./assets/fonts (see README). Icons come from two fonts:
  * CastleIcon.ttf      - Castle's own icon font (codepoints below)
  * MaterialSymbols-*   - Material Symbols, weight 200, unfilled (filled for lock);
                          only used where Castle has no matching icon.
"""
from __future__ import annotations

from pathlib import Path

import flet as ft

# ── palette ──────────────────────────────────────────────────────────────
MAJOR = "#ffffff"      # major text
MINOR1 = "#888888"     # minor text 1
SEP = "#444444"        # separator
BG = "#000000"         # background
# Castle's other colors (#777, pink, yellow, green, blue) are rare there, so they're not used here.
ERROR = MAJOR            # errors are plain white text: the accents stay unused

# ── fonts ────────────────────────────────────────────────────────────────
ASSETS = Path(__file__).resolve().parent / "assets"
FONT_FILES = {
    "Basteleur": "fonts/Basteleur-Bold.otf",
    "CastleIcons": "fonts/CastleIcon.ttf",
    "DMSans": "fonts/DMSans-Regular.ttf",
    "DMSans-Medium": "fonts/DMSans-Medium.ttf",
    "DMSans-Bold": "fonts/DMSans-Bold.ttf",
    "MSOutlined": "fonts/MaterialSymbols-W200.ttf",
    "MSFilled": "fonts/MaterialSymbols-W200-Filled.ttf",
}
BODY = "DMSans"
TITLE = "Basteleur"


ICON_FILES = ("icons/key-info.png", "icons/default-profile.png")


def missing_icons() -> list[str]:
    if not ASSETS.exists():
        return []
    return [rel for rel in ICON_FILES if not (ASSETS / rel).exists()]


def missing_fonts() -> list[str]:
    """Font files absent from assets/ (only checkable when assets/ is on disk,
    i.e. when running from source rather than a packaged build)."""
    if not ASSETS.exists():
        return []
    return [rel for rel in FONT_FILES.values() if not (ASSETS / rel).exists()]


def font_map() -> dict[str, str]:
    """Fonts to register with the page: only ones that exist."""
    gone = set(missing_fonts())
    return {fam: rel for fam, rel in FONT_FILES.items() if rel not in gone}


# ── Castle icon font ─────────────────────────────────────────────────────
CASTLE = {
    "space": 0x0020, "operator": 0xE900, "operator_done": 0xE901, "menu": 0xE902,
    "announcement": 0xE903, "announcement_thick": 0xE904, "left_arrow": 0xE905,
    "notifications": 0xE906, "notifications_active": 0xE907, "notifications_off": 0xE908,
    "brick": 0xE909, "snapshot": 0xE90A, "up_arrowhead": 0xE90B, "puzzle_piece": 0xE90C,
    "puzzle_piece_thick": 0xE90D, "check": 0xE90E, "paste": 0xE90F, "copy": 0xE910,
    "close": 0xE911, "comment": 0xE912, "comment_thick": 0xE913, "download": 0xE914,
    "dislike": 0xE915, "dislike_filled": 0xE916, "pencil": 0xE917, "circle": 0xE918,
    "squiggle": 0xE919, "line": 0xE91A, "square": 0xE91B, "triangle": 0xE91C,
    "code": 0xE91D, "broken_chair": 0xE91E, "dweller": 0xE91F, "key": 0xE920,
    "magic_wand": 0xE921, "eraser": 0xE922, "experiment": 0xE923, "pipette": 0xE924,
    "bucket": 0xE925, "react": 0xE926, "react_thick": 0xE927, "react_filled": 0xE928,
    "font_bore": 0xE929, "font_courier_prime": 0xE92A, "font_overlay": 0xE92B,
    "font_glacier": 0xE92C, "font_helico_centrica": 0xE92D, "font_piazzolla": 0xE92E,
    "font_synco": 0xE92F, "font_tektur": 0xE930, "font_yatra_one": 0xE931,
    "fork": 0xE932, "goto_editor": 0xE933, "goto_scene": 0xE934, "grab": 0xE935,
    "sand_timer": 0xE936, "download2": 0xE937, "drums": 0xE938, "upload_cloud": 0xE939,
    "microphone": 0xE93A, "spark": 0xE93B, "music": 0xE93C, "start_party": 0xE93D,
    "major_changes": 0xE93E, "major_changes_thick": 0xE93F, "book": 0xE940,
    "copy_link": 0xE941, "horizontal_line": 0xE942, "muted": 0xE943,
    "onionskin_off": 0xE944, "onionskin_on": 0xE945, "more": 0xE946, "flower": 0xE947,
    "pause": 0xE948, "play": 0xE949, "play_filled": 0xE94A, "play_music": 0xE94B,
    "players": 0xE94C, "asterisk_players": 0xE94D, "return_players": 0xE94E,
    "follow_needle": 0xE94F, "add": 0xE950, "missing": 0xE951, "qr_code": 0xE952,
    "redo": 0xE953, "swap": 0xE954, "swap_thick": 0xE955, "reload": 0xE956,
    "stop": 0xE957, "bookmark": 0xE958, "bookmark_filled": 0xE959, "search": 0xE95A,
    "settings": 0xE95B, "settings_cloud_saved": 0xE95C, "share": 0xE95D, "upload": 0xE95E,
    "snapchat": 0xE95F, "delete": 0xE960, "twitter_x": 0xE961, "undo": 0xE962,
    "unmuted": 0xE963, "volume": 0xE964, "record": 0xE965, "picture": 0xE966,
    "sms": 0xE967, "people": 0xE968,
}

# ── Material Symbols (only where Castle has no equivalent) ───────────────
# Codepoints are stable; tools/make_icon_fonts.py subsets the font to exactly these.
MD = {
    "calendar_today": 0xE935,
    "globe": 0xE64C,
    "link": 0xE250,
    "lock": 0xE899,
    "logout": 0xE9BA,
    "login": 0xEA77,
    "folder_open": 0xE2C8,
    "menu": 0xE5D2,
    # text-actor alignment grid
    "arrow_insert": 0xF837,
    "arrow_upward": 0xE5D8,
    "arrow_outward": 0xF8CE,
    "arrow_back": 0xE5C4,
    "center_focus_weak": 0xE3B5,
    "arrow_forward": 0xE5C8,
    "arrow_downward": 0xE5DB,
}


def castle_icon(name: str, size: float = 22, color: str = MAJOR) -> ft.Text:
    cp = CASTLE[name]
    return ft.Text(chr(cp), font_family="CastleIcons", size=size, color=color, no_wrap=True)


def md_icon(name: str, size: float = 18, color: str = MINOR1, filled: bool = False) -> ft.Text:
    """Material Symbols, weight 200. Unfilled unless `filled` (lock = filled)."""
    return ft.Text(chr(MD[name]), font_family="MSFilled" if filled else "MSOutlined",
                   size=size, color=color, no_wrap=True)


def visibility_icon(visibility: str | None, size: float = 18, color: str = MINOR1) -> ft.Text:
    v = (visibility or "").lower()
    if v == "public":
        return md_icon("globe", size, color)
    if v == "unlisted":
        return md_icon("link", size, color)
    return md_icon("lock", size, color, filled=True)   # private (and unknown)


# ── text helpers ─────────────────────────────────────────────────────────
def text(value, size: float = 14, color: str = MAJOR, bold: bool = False, medium: bool = False, **kw) -> ft.Text:
    fam = "DMSans-Bold" if bold else "DMSans-Medium" if medium else BODY
    return ft.Text(str(value), size=size, color=color, font_family=fam, **kw)


def minor(value, size: float = 12, color: str = MINOR1, **kw) -> ft.Text:
    return text(value, size=size, color=color, **kw)


def title(value, size: float = 22, **kw) -> ft.Text:
    """Basteleur Bold: major titles."""
    return ft.Text(str(value), size=size, color=MAJOR, font_family=TITLE, **kw)


def separator() -> ft.Divider:
    return ft.Divider(height=1, thickness=1, color=SEP)


def make_theme() -> ft.Theme:
    return ft.Theme(font_family=BODY, color_scheme_seed=MAJOR,
                    color_scheme=ft.ColorScheme(primary=MAJOR, on_primary=BG, surface=BG, on_surface=MAJOR))


def popup_shape(radius: float = 14) -> ft.RoundedRectangleBorder:
    return ft.RoundedRectangleBorder(radius=radius, side=ft.BorderSide(1, SEP))

# ── extras ───────────────────────────────────────────────────────────────
DANGER = "#F87171"       # discard / destructive text
APP_VERSION = "0.5.0"
CASTLE_LINKS = [
    ("Colophon", "https://castle.xyz/colophon"),
    ("Terms", "https://castle.xyz/terms"),
    ("Privacy", "https://castle.xyz/privacy_policy"),
    ("Github", "https://github.com/MGoosePlayZ/castletool-android"),
]
DISCLAIMER = ("Castletool is an independent, unofficial app. It is not affiliated with, endorsed by, "
              "or sponsored by Castle or Montarey's Coast, Inc.")
