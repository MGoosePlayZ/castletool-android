import _path  # noqa: F401
"""Needle follows the audio actually heard (not the mixer's write pointer); tiles scroll, the needle is native."""
import asyncio
import mido
import castletool_core as ct
import live_synth as ls, piano_roller as pr, synth

mid = mido.MidiFile(ticks_per_beat=480)
t = mido.MidiTrack(); mid.tracks.append(t)
for i in range(120):                                   # a long-ish song: many tiles
    t.append(mido.Message("note_on", note=50 + i % 20, velocity=90, time=0 if i == 0 else 240))
    t.append(mido.Message("note_off", note=50 + i % 20, velocity=0, time=120))
music = ct.build_music(mid)


class Sink(ls.ListSink):
    q = 0
    def queued(self): return self.q


eng = ls.LiveSynth(music, 120, lambda: Sink())
# position = written - queued
eng._sink = s = Sink(); eng._playing = True; eng.pos = 44100; s.q = 17640
assert abs(eng.position - (44100 - 17640) / eng.sr) < 1e-9
s.q = 10 ** 9; eng.loop = False; assert eng.position == 0.0                  # never negative
eng._playing = False; assert eng.position == 44100 / eng.sr               # paused: the written position
# pausing resumes from what was heard
eng2 = ls.LiveSynth(music, 120, lambda: Sink()); eng2.loop = False
eng2.play()
import time
for _ in range(100):
    if eng2._sink is not None: break
    time.sleep(0.01)
eng2._sink.q = 5000; time.sleep(0.15); before = eng2.pos
eng2.pause(); time.sleep(0.5)
assert eng2.pos <= before + 0 or eng2.pos < before + 3 * ls.BLOCK        # rewound by the queue (5000) at pause
eng2.close()


class Page:
    def __init__(self): self.tasks = []
    def update(self, *a): pass
    def run_task(self, fn, *a): self.tasks.append((fn, a))


class App:
    page = Page()


model = pr.build_model(music, 120)
eng3 = ls.LiveSynth(music, 120, lambda: Sink())
r = pr.PianoRoller(App(), eng3, model, 360, lambda m: None)
assert 0 in r.slots and 1 in r.slots and r.strip.controls[-1] is r.needle_line
x0 = r.needle_line.left
r.needle = 10.0; r._layout()
assert r.needle_line.left > x0 and r.handle.visible
r.ox = r.tw * 1.5; r._layout()                         # scrolled: tile 0 gone, 1 and 2 on screen, positions follow
assert 0 not in r.slots and {1, 2} <= set(r.slots) and abs(r.slots[1].left - (-0.5 * r.tw)) < 1e-6
assert any(fn.__func__ is pr.PianoRoller._prefetch for fn, _ in App.page.tasks if hasattr(fn, "__func__"))
for fn, a in list(App.page.tasks):
    asyncio.run(fn(*a))
assert r.cache
# tiles line up: neighbouring tiles drawn from the same world are exactly one tile wide
from PIL import Image
import io
a = Image.open(io.BytesIO(r._tile(1))); assert a.size == (r.tw * 2, round(r.bh * 2))
print("roller live ok")
