import time, threading
import numpy as np, mido
import castletool_core as ct, synth, live_synth

mid = mido.MidiFile(ticks_per_beat=480)
tr = mido.MidiTrack(); mid.tracks.append(tr)
tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120)))
tr.append(mido.Message("program_change", program=80, channel=0, time=0))
for i, n in enumerate((69, 69, 72)):
    tr.append(mido.Message("note_on", note=n, velocity=100, time=0 if i == 0 else 480)); tr.append(mido.Message("note_off", note=n, velocity=0, time=10))
dr = mido.MidiTrack(); mid.tracks.append(dr)
dr.append(mido.Message("note_on", note=38, velocity=110, channel=9, time=0))
music = ct.build_music(mid); bpm = ct.get_bpm(mid)
st = synth.music_stats(music, bpm)
assert st["tracks"] == 2 and st["notes"] == 4 and abs(st["seconds"] - 980/480*0.5) < 1e-3, st

# play to the end (no loop) into a memory sink
sink = live_synth.ListSink()
ended = threading.Event()
eng = live_synth.LiveSynth(music, bpm, lambda: sink, on_end=ended.set); eng.loop = False
eng.play(); assert ended.wait(10), "never ended"
pcm = np.frombuffer(b"".join(sink.chunks), dtype="<i2")
assert abs(len(pcm) / 22050 - eng.duration) < 0.1 and abs(pcm).max() > 2000 and abs(pcm).max() < 32767
seg = pcm[:int(0.05*22050)].astype(float)           # A4 square lead -> ~440 Hz
zc = np.sum(np.diff(np.sign(seg)) != 0) / 2 / 0.05; assert 400 < zc < 480, zc
eng.close(); assert sink.closed

# pause / seek / loop with a realtime sink
sink2 = live_synth.ListSink(realtime=True)
eng2 = live_synth.LiveSynth(music, bpm, lambda: sink2); eng2.loop = True
eng2.play(); time.sleep(0.4); p1 = eng2.position; assert 0.2 < p1 < 0.8, p1
eng2.pause(); time.sleep(0.3); p2 = eng2.position; time.sleep(0.3); assert abs(eng2.position - p2) < 0.1
eng2.seek(1.0); time.sleep(0.1); assert eng2.position >= 1.0
eng2.play(); time.sleep(0.3); assert eng2.position > 1.1
eng2.close()
# no audio device -> error surfaced, not raised in UI thread
class Bad(live_synth.Sink):
    def open(self, sr): raise live_synth.AudioUnavailable("nope")
eng3 = live_synth.LiveSynth(music, bpm, Bad); eng3.play(); time.sleep(0.3); assert eng3.error == "nope" and not eng3.playing
# mute track
music["song"]["tracks"][0]["instrument"]["props"]["muted"] = True
assert len(live_synth.LiveSynth(music, bpm, lambda: sink).notes) == 1
print("synth ok")
