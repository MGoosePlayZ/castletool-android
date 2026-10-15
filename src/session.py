"""Per-card editing session: dirty tracking, undo/redo, discard.

The card's files on disk are the working copy. A snapshot is every file of
card.json + scene/. `saved` is the last state known to be on Castle (the state
when the card was opened, or after Save). Edits are dirty while the files
differ from `saved`.

While dirty, `saved` is also kept on disk (.castle/baseline-<card>.zip) so
Discard still works, and unsaved edits aren't silently replaced, after the app
was closed. Undo/redo history lives only in memory (cleared on app close).
"""
from __future__ import annotations

import hashlib
import io
import zipfile
import zlib
from pathlib import Path
from typing import Optional

HISTORY = 25


def snapshot(card_dir: Path) -> dict[str, bytes]:
    card_dir = Path(card_dir)
    snap: dict[str, bytes] = {}
    cj = card_dir / "card.json"
    if cj.exists():
        snap["card.json"] = cj.read_bytes()
    scene = card_dir / "scene"
    if scene.is_dir():
        for p in sorted(scene.rglob("*")):
            if p.is_file():
                snap[p.relative_to(card_dir).as_posix()] = p.read_bytes()
    return snap


def digest(snap: dict[str, bytes]) -> str:
    h = hashlib.sha1()
    for rel in sorted(snap):
        h.update(rel.encode())
        h.update(hashlib.sha1(snap[rel]).digest())
    return h.hexdigest()


def restore(card_dir: Path, snap: dict[str, bytes]) -> None:
    card_dir = Path(card_dir)
    scene = card_dir / "scene"
    if scene.is_dir():
        for p in scene.rglob("*"):
            if p.is_file() and p.relative_to(card_dir).as_posix() not in snap:
                p.unlink()
    for rel, data in snap.items():
        p = card_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    (card_dir / "scene" / "blueprints").mkdir(parents=True, exist_ok=True)


def _pack(snap: dict[str, bytes]) -> dict[str, bytes]:
    return {k: zlib.compress(v, 1) for k, v in snap.items()}


def _unpack(packed: dict[str, bytes]) -> dict[str, bytes]:
    return {k: zlib.decompress(v) for k, v in packed.items()}


class CardSession:
    def __init__(self, card_dir: Path):
        self.dir = Path(card_dir)
        self.baseline_path = self.dir.parent.parent / ".castle" / f"baseline-{self.dir.name}.zip"
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        if self.baseline_path.exists():
            self.saved = self._load_baseline()
        else:
            self.saved = snapshot(self.dir)
        self.saved_digest = digest(self.saved)
        self.dirty = False
        self.refresh_dirty()

    # persistence of the saved state while dirty
    def _load_baseline(self) -> dict[str, bytes]:
        with zipfile.ZipFile(self.baseline_path) as z:
            return {n: z.read(n) for n in z.namelist()}

    def _write_baseline(self) -> None:
        self.baseline_path.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, data in self.saved.items():
                z.writestr(rel, data)
        self.baseline_path.write_bytes(buf.getvalue())

    # state
    def refresh_dirty(self) -> bool:
        self.dirty = digest(snapshot(self.dir)) != self.saved_digest
        if self.dirty and not self.baseline_path.exists():
            self._write_baseline()
        elif not self.dirty and self.baseline_path.exists():
            self.baseline_path.unlink()
        return self.dirty

    @property
    def can_undo(self) -> bool:
        return bool(self.undo_stack)

    @property
    def can_redo(self) -> bool:
        return bool(self.redo_stack)

    # editing
    def checkpoint(self) -> None:
        """Call right before changing the card's files."""
        self.undo_stack.append(_pack(snapshot(self.dir)))
        del self.undo_stack[:-HISTORY]
        self.redo_stack.clear()

    def cancel_checkpoint(self) -> None:
        """The change failed: forget the checkpoint taken for it."""
        if self.undo_stack:
            self.undo_stack.pop()

    def undo(self) -> bool:
        if not self.undo_stack:
            return False
        self.redo_stack.append(_pack(snapshot(self.dir)))
        restore(self.dir, _unpack(self.undo_stack.pop()))
        self.refresh_dirty()
        return True

    def redo(self) -> bool:
        if not self.redo_stack:
            return False
        self.undo_stack.append(_pack(snapshot(self.dir)))
        restore(self.dir, _unpack(self.redo_stack.pop()))
        self.refresh_dirty()
        return True

    def discard(self) -> None:
        restore(self.dir, self.saved)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.refresh_dirty()

    def mark_saved(self) -> None:
        self.saved = snapshot(self.dir)
        self.saved_digest = digest(self.saved)
        self.refresh_dirty()


def deck_has_unsaved(deck_dir: Path) -> bool:
    """True if any card of this deck has edits that were never saved to Castle."""
    meta = Path(deck_dir) / ".castle"
    return meta.is_dir() and any(meta.glob("baseline-*.zip"))
