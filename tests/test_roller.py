import _path  # noqa: F401  (puts src/ on sys.path)
"""Vector arcs (Castle's sagitta arcs), per-track pattern colors, and the Music tab's piano roller."""
import colorsys, math
import mido
import castletool_core as ct
import drawing, piano_roller, synth

# arcs: bulge = bend point's distance from the chord, capped at a semicircle
pts = drawing.segment_points({"p": [0, 0, 2, 0], "bp": {"x": 1, "y": 0.5}})
assert abs(abs(pts[len(pts) // 2][1]) - 0.5) < 1e-6 and pts[0] == (0, 0) and pts[-1] == (2, 0)
far = drawing.segment_points({"p": [0, 0, 2, 0], "bp": {"x": 1, "y": 9}})
assert max(abs(y) for _, y in far) <= 1 + 1e-9                       # never past a semicircle
assert drawing.segment_points({"p": [0, 0, 2, 0], "bp": {"x": 1, "y": 0}}) == [(0, 0), (2, 0)]
q = drawing.segment_points({"p": [0, 0, 2, 2], "s": 2})              # quarter arc, bulging toward smaller y
assert abs(q[12][0] - 1.4142) < 1e-3 and abs(q[12][1] - 0.5858) < 1e-3
assert drawing.segment_points({"p": [0, 0, 2, 0], "s": 1}) == [(0, 0), (2, 0)]

# every imported track gets its own vibrant color
for _ in range(500):
    c = ct.make_color()
    h, l, s = colorsys.rgb_to_hls(c["r"], c["g"], c["b"])
    assert 0.2995 <= l <= 0.7005 and s >= 0.6995, c
mid = mido.MidiFile(ticks_per_beat=480)
for prog in (0, 30, 60):
    t = mido.MidiTrack(); mid.tracks.append(t)
    t.append(mido.Message("program_change", program=prog, time=0))
    for i in range(8):
        t.append(mido.Message("note_on", note=50 + i, velocity=90, time=0 if i == 0 else 240))
        t.append(mido.Message("note_off", note=50 + i, velocity=0, time=120))
music = ct.build_music(mid)
cols = synth.track_colors(music)
assert len(cols) == 3 and len(set(cols)) == 3

# piano roller: model + frame
model = piano_roller.build_model(music, 120)
assert model.tonal == 8 and model.row_count == 8 and not model.has_drums
png = piano_roller.render_png(model, 300, 300 / piano_roller.RATIO, 0, 5)
assert png[:4] == b"\x89PNG"
assert piano_roller.build_model({"song": {"tracks": [], "patterns": {}}}, 120) is None
print("roller ok")
