import _path  # noqa: F401  (puts src/ on sys.path)
"""Music -> Sound conversion and MP3 export (upload is mocked)."""
import json, tempfile
from pathlib import Path
import mido
import blueprint_ops as ops
import castletool_core as ct
import media_av

if not media_av.can_encode_mp3():
    print("skip: no mp3 encoder in this PyAV build"); raise SystemExit(0)

mid = mido.MidiFile(ticks_per_beat=480)
tr = mido.MidiTrack(); mid.tracks.append(tr)
tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120)))
for i, n in enumerate((60, 64, 67)):
    tr.append(mido.Message("note_on", note=n, velocity=100, time=0 if i == 0 else 480))
    tr.append(mido.Message("note_off", note=n, velocity=0, time=10))
music = ct.build_music(mid)

logs = []
uploads = []
ct.upload_audio_file = lambda p: (uploads.append(Path(p).stat().st_size), {"fileId": "f", "url": f"https://x/{len(uploads)}.mp3"})[1]

with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    card = t / "deck" / "cards" / "c1"; (card / "scene/blueprints").mkdir(parents=True)
    (card / "card.json").write_text(json.dumps({"cardId": "c1", "sceneProperties": {"clock": {"tempo": 120}}}))
    bp = card / "scene/blueprints/hero.json"
    bp.write_text(json.dumps({"entryId": "e1", "entryType": "actorBlueprint", "title": "Hero",
                              "actorBlueprint": {"components": {"Music": music}}}))
    b = ops.load_blueprint(bp); assert ops.has_music(b) and not ops.audio_urls(b)

    # download: a real mp3 with sane length
    data = ops.export_music_mp3(bp, card, log=logs.append)
    assert data[:3] == b"ID3" or data[0] == 0xFF, data[:4]
    out = t / "o.mp3"; out.write_bytes(data)
    assert 1.5 < media_av.probe_duration(out) < 6, media_av.probe_duration(out)
    assert ops.has_music(ops.load_blueprint(bp)), "export must not touch the blueprint"

    # convert: music gone, sound rule present
    ops.music_to_sound(bp, card, log=logs.append, token="tok")
    b = ops.load_blueprint(bp)
    assert not ops.components(b).get("Music") and len(ops.audio_urls(b)) == 1, ops.components(b).keys()
    assert uploads, "nothing uploaded"

    # convert again over an existing sound: old rule replaced, not stacked
    ops.components(b)["Music"] = music; ops.write_blueprint(bp, b)
    ops.music_to_sound(bp, card, log=logs.append, token="tok")
    b = ops.load_blueprint(bp)
    assert len(ops.audio_urls(b)) == 1 and not ops.components(b).get("Music")

    # failed upload: blueprint untouched, Music kept
    ops.components(b)["Music"] = music; ops.write_blueprint(bp, b)
    def boom(p): raise RuntimeError("net down")
    ct.upload_audio_file = boom
    try:
        ops.music_to_sound(bp, card, log=logs.append, token="tok"); assert False
    except ops.OpError as e:
        assert "net down" in str(e), e
    assert ops.has_music(ops.load_blueprint(bp))

    # not signed in
    try:
        ops.music_to_sound(bp, card, log=logs.append); assert False
    except ops.OpError as e:
        assert "Sign in" in str(e)
print("ok")
