import _path  # noqa: F401  (puts src/ on sys.path)
"""Offline tests for drawing + blueprint ops using castletool's own converters."""
import json, io, tempfile, base64
from pathlib import Path
from PIL import Image, ImageDraw
import mido
import blueprint_ops as ops
import drawing, castletool_core as ct

logs = []
log = logs.append
with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    card = t / "deck" / "cards" / "c1"; (card / "scene/blueprints").mkdir(parents=True)
    (card.parent.parent / "deck.json").write_text(json.dumps({"deckId": "d", "initialCard": {"cardId": "c1", "backgroundColor": "#112233"}, "cards": [{"cardId": "c1"}]}))
    (card / "card.json").write_text(json.dumps({"cardId": "c1", "backgroundColor": "#112233", "sceneProperties": {}}))
    bp = card / "scene/blueprints/hero.json"
    bp.write_text(json.dumps({"entryId": "e1", "entryType": "actorBlueprint", "title": "Hero", "actorBlueprint": {"components": {}}}))

    # --- image
    png = t / "a.png"; im = Image.new("RGBA", (40, 20), (255, 0, 0, 255)); ImageDraw.Draw(im).rectangle([0,0,9,19], fill=(0,255,0,255)); im.save(png)
    info = ops.inspect_media(png, log=log); assert info["category"] == "image", info
    ops.import_media(bp, card, png, {"quantize": 8}, log=log)
    b = ops.load_blueprint(bp); d2 = drawing.get_drawing2(b)
    assert d2 and drawing.drawing_info(d2)["bitmap"]
    out = Image.open(io.BytesIO(drawing.render_png(b, 100))).convert("RGBA")
    assert out.getpixel((50, 50))[:3] == (255, 0, 0) or out.getpixel((50,50))[0] > 200, out.getpixel((50,50))
    assert out.getpixel((10, 50))[1] > 200  # green strip left side

    # --- svg vector: line thickness ~0.28 units
    svg = t / "l.svg"; svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><line x1="0" y1="128" x2="256" y2="128" stroke="#00ff00" stroke-width="1"/></svg>')
    ops.import_media(bp, card, svg, {"svg_style": "vector"}, log=log)
    b = ops.load_blueprint(bp); d2 = drawing.get_drawing2(b)
    inf = drawing.drawing_info(d2); assert inf["vector"] and inf["segments"] >= 1, inf
    xs = [x for fr in d2["drawData"]["layers"][0]["frames"] for sg in fr["pathDataList"] for x in (sg["p"][0], sg["p"][2])]
    assert abs((max(xs) - min(xs)) - 100) < 1, (min(xs), max(xs))   # scale 1 -> x10: 256px = 100 units
    ops.import_media(bp, card, svg, {"svg_style": "vector", "svg_scale": 0.1}, log=log)
    b = ops.load_blueprint(bp); d2 = drawing.get_drawing2(b)
    img = Image.open(io.BytesIO(drawing.render_png(b, 400))).convert("RGBA")
    col = [img.getpixel((200, y))[3] for y in range(400)]
    thickness_px = sum(1 for a in col if a > 128)
    # line is 10 units long (256px / 25.6) fitted into ~352px => k≈35 px/unit; 0.28*35≈10px
    assert 7 <= thickness_px <= 13, thickness_px

    # --- gif animation frames
    gif = t / "g.gif"; fr = [Image.new("RGB", (16, 16), c) for c in ((255,0,0),(0,255,0),(0,0,255))]
    fr[0].save(gif, save_all=True, append_images=fr[1:], duration=100, loop=0)
    ops.import_media(bp, card, gif, {}, log=log)
    d2 = drawing.get_drawing2(ops.load_blueprint(bp)); assert drawing.frame_count(d2) == 3
    assert len(drawing.render_animation(d2, 64)) == 3

    # --- video (PyAV; no ffmpeg binary)
    if ct.HAS_FFMPEG:
        mp4 = _path.make_test_video(t / "v.mp4")
        ops.import_media(bp, card, mp4, {"want_audio": False}, log=log)
        assert drawing.frame_count(drawing.get_drawing2(ops.load_blueprint(bp))) >= 2

    # --- multiple files → separate actors (fork)
    zp = t / "pics.zip"
    import zipfile
    with zipfile.ZipFile(zp, "w") as z:
        z.write(png, "one.png"); z.write(png, "two.png")
    w = ops.import_media(bp, card, zp, {"layout": "separate"}, log=log)
    assert len(w) == 2 and any(p.name != "hero.json" for p in w), w
    assert len(list((card/"scene/blueprints").glob("*.json"))) == 2

    # --- font (vector + bitmap) with DejaVu
    ttf = _path.make_test_font(Path(tempfile.mkdtemp()) / "t.ttf")
    ops.import_font(bp, card, ttf, {"charsets": ["Numeric"], "color": [1,1,1,1]}, log=log)
    d2 = drawing.get_drawing2(ops.load_blueprint(bp)); assert drawing.frame_count(d2) == 10 and drawing.drawing_info(d2)["vector"]
    assert drawing.render_png(d2, 64)
    ops.import_font(bp, card, ttf, {"custom": "A B", "bitmap": True, "px": 32, "color": [1,1,1,1]}, log=log)
    d2 = drawing.get_drawing2(ops.load_blueprint(bp)); assert drawing.frame_count(d2) == 2 and drawing.drawing_info(d2)["bitmap"]

    # --- midi
    mid = mido.MidiFile(ticks_per_beat=480); tr = mido.MidiTrack(); mid.tracks.append(tr)
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(90)))
    for n in (60, 64, 67):
        tr.append(mido.Message("note_on", note=n, velocity=90, time=0 if n == 60 else 480)); tr.append(mido.Message("note_off", note=n, velocity=0, time=240))
    mp = t / "s.mid"; mid.save(mp)
    music = ops.import_midi(bp, card, mp, log=log)
    assert ops.card_tempo(card) == 90.0 and "Music" in ops.components(ops.load_blueprint(bp))

    # --- audio rule url parsing (without upload)
    b = ops.load_blueprint(bp); ops.ct.add_or_replace_audio_rule(b, ["https://x/a.mp3", "https://x/b.mp3"])
    assert ops.audio_urls(b) == ["https://x/a.mp3", "https://x/b.mp3"]
    try:
        ops.import_audio(bp, card, png, log=log); assert False
    except ops.OpError as e: assert "Sign in" in str(e)

    # previews
    pv = ops.preview_media(png, {"scale": 2}, log=log, bg="#000000"); assert pv and pv[:4] == b"\x89PNG"
    pf = ops.preview_font(ttf, {"charsets": ["Numeric"], "color": [1,1,1,1]}, log=log); assert pf
    pf2 = ops.preview_font(ttf, {"charsets": ["Numeric"], "bitmap": True, "px": 32, "color": [1,1,1,1]}, log=log); assert pf2
    # --- bg color mirrors into deck.json
    assert ops.set_card_background(card, "ff8800") == "#ff8800"
    dj = json.loads((card.parent.parent / "deck.json").read_text())
    assert dj["initialCard"]["backgroundColor"] == "#ff8800" and dj["cards"][0]["backgroundColor"] == "#ff8800"
    assert json.loads((card / "card.json").read_text())["sceneProperties"]["backgroundColor"]["r"] == 1
print("ops ok")
