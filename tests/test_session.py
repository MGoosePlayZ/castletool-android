import _path  # noqa: F401  (puts src/ on sys.path)
import json, tempfile
from pathlib import Path
import session as S

t = Path(tempfile.mkdtemp()); deck = t / "deck"; card = deck / "cards/c1"; (card / "scene/blueprints").mkdir(parents=True)
(card / "card.json").write_text('{"a":1}'); (card / "scene/blueprints/x.json").write_text('{"v":1}')
s = S.CardSession(card); assert not s.dirty and not s.can_undo
def edit(v):
    s.checkpoint(); (card / "scene/blueprints/x.json").write_text('{"v":%d}' % v); s.refresh_dirty()
edit(2); assert s.dirty and s.can_undo and S.deck_has_unsaved(deck)
edit(3); (card / "scene/blueprints/new.json").write_text("{}"); s.refresh_dirty()
assert s.undo(); assert json.loads((card / "scene/blueprints/x.json").read_text())["v"] == 2 and s.can_redo
assert s.undo(); assert not s.dirty and not S.deck_has_unsaved(deck)       # back to saved state => clean
assert s.redo(); assert s.dirty
# restart simulation: new session sees the unsaved baseline, discard restores it
s2 = S.CardSession(card); assert s2.dirty
(card / "scene/blueprints/extra.json").write_text("{}")
s2.discard(); assert not s2.dirty and json.loads((card / "scene/blueprints/x.json").read_text())["v"] == 1
assert not (card / "scene/blueprints/extra.json").exists() and not S.deck_has_unsaved(deck)
# save => new baseline; undo past save is dirty again
s3 = S.CardSession(card); s3.checkpoint(); (card / "scene/blueprints/x.json").write_text('{"v":9}'); s3.refresh_dirty(); assert s3.dirty
s3.mark_saved(); assert not s3.dirty and s3.can_undo
s3.undo(); assert s3.dirty
# failed op drops its checkpoint
s3.mark_saved(); n = len(s3.undo_stack); s3.checkpoint(); s3.cancel_checkpoint(); assert len(s3.undo_stack) == n
print("session ok")
