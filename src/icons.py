"""Small local preview icons for blueprints (128x128, initial frame only).

They exist purely to keep the app snappy: lists show these instead of decoding
a blueprint's full-size frames. They are NOT uploaded to Castle and are kept
OUT of the deck folder, in a sibling folder next to it:

    decks/<deckId>/              <- the deck (what gets pushed to Castle)
    decks/<deckId>-icons/        <- these previews (safe to delete any time)
        <cardId>/index.json      <- blueprint title / has-drawing / which icon is current
        <cardId>/<blueprint>.png

An entry is current while the blueprint file's (mtime, size) are unchanged;
any edit (import, undo, re-clone...) makes it stale and it is regenerated.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import blueprint_ops as ops
import drawing
import tasks

ICON_SIZE = 128


class IconStore:
    def __init__(self, decks_dir: Path):
        self.decks_dir = Path(decks_dir)

    # ── paths ─────────────────────────────────────────────────────────────
    def folder(self, deck_dir: Path) -> Path:
        return self.decks_dir / f"{Path(deck_dir).name}-icons"

    def _card_folder(self, card_dir: Path) -> Path:
        card_dir = Path(card_dir)
        return self.folder(card_dir.parent.parent) / card_dir.name

    def _load(self, card_dir: Path) -> dict:
        try:
            return json.loads((self._card_folder(card_dir) / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, card_dir: Path, index: dict) -> None:
        folder = self._card_folder(card_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _key(bp_path: Path) -> str:
        st = Path(bp_path).stat()
        return f"{st.st_mtime_ns}-{st.st_size}"

    # ── lookups ───────────────────────────────────────────────────────────
    def peek(self, card_dir: Path, bp_path: Path) -> Optional[dict]:
        """The index entry if it is still current, else None. No blueprint parsing."""
        e = self._load(card_dir).get(Path(bp_path).stem)
        return e if e and e.get("key") == self._key(bp_path) else None

    def meta(self, card_dir: Path, bp_path: Path) -> dict:
        """Current entry; reads the blueprint (for its title) only when stale. The icon
        itself is NOT rendered here: entry["done"] is False until `render` has run."""
        e = self.peek(card_dir, bp_path)
        if e:
            return e
        bp_path = Path(bp_path)
        bp = ops.load_blueprint(bp_path)
        d2 = drawing.get_drawing2(bp)
        e = {"key": self._key(bp_path), "title": bp.get("title") or bp_path.stem,
             "has_drawing": bool(d2 and drawing.frame_count(d2)), "done": False}
        index = self._load(card_dir)
        index[bp_path.stem] = e
        self._save(card_dir, index)
        return e

    def png(self, card_dir: Path, bp_path: Path) -> Optional[bytes]:
        """Bytes of the current icon, or None if it hasn't been rendered."""
        e = self.peek(card_dir, bp_path)
        if not e or not e.get("done") or not e.get("has_drawing"):
            return None
        try:
            return (self._card_folder(card_dir) / f"{Path(bp_path).stem}.png").read_bytes()
        except OSError:
            return None

    def render(self, card_dir: Path, bp_path: Path) -> Optional[bytes]:
        """Make (or refresh) the 128x128 icon from the blueprint's INITIAL frame only,
        on a transparent background. Blocking: call from a worker thread."""
        bp_path = Path(bp_path)
        tasks.check()
        e = self.meta(card_dir, bp_path)
        if not e["has_drawing"]:
            return None
        if e.get("done"):
            return self.png(card_dir, bp_path)
        bp = ops.load_blueprint(bp_path)
        tasks.check()
        data = drawing.render_png(bp, ICON_SIZE, None)       # initial frame, no background
        if data is None:
            return None
        folder = self._card_folder(card_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{bp_path.stem}.png").write_bytes(data)
        index = self._load(card_dir)
        index[bp_path.stem] = {**e, "done": True}
        self._save(card_dir, index)
        return data

    def prune(self, card_dir: Path, keep_stems: set) -> None:
        """Forget icons of blueprints that no longer exist."""
        index = self._load(card_dir)
        gone = [s for s in index if s not in keep_stems]
        for s in gone:
            index.pop(s, None)
            (self._card_folder(card_dir) / f"{s}.png").unlink(missing_ok=True)
        if gone:
            self._save(card_dir, index)

    def delete_deck(self, deck_dir: Path) -> None:
        shutil.rmtree(self.folder(deck_dir), ignore_errors=True)
