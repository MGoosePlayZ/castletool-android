import _path  # noqa: F401
import tempfile, wave
from pathlib import Path
import numpy as np
import media_av as m

assert m.HAS_AV, "PyAV is required"
t = Path(tempfile.mkdtemp())
mp4 = _path.make_test_video(t / "v.mp4", frames=10, size=(64, 48), fps=10)
assert m.probe_video_dimensions(mp4) == (64, 48)
fr, fps = m.extract_mp4_frames(mp4, 32, 24, 2, False)
assert 4 <= len(fr) <= 6 and abs(fps - 5) < 0.1, (len(fr), fps)

if m.can_encode_mp3():
    sr = 44100; n = sr * 70
    x = (np.sin(2 * np.pi * 440 * np.arange(n) / sr) * 12000).astype("<i2")
    with wave.open(str(t / "a.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(x.tobytes())
    parts = m.split_audio_mp3(t / "a.wav", t / "out", False, chunk_seconds=30, segment_seconds=29)
    assert len(parts) == 3 and all(m.probe_duration(p) <= 30.5 for p in parts)
    big = t / "long.mp3"; m.extract_audio_mp3(t / "a.wav", big)                 # re-encode once, then split WITHOUT re-encoding
    parts2 = m.split_audio_mp3(big, t / "out2", False, chunk_seconds=30, segment_seconds=29)
    assert len(parts2) == 3 and sum(m.probe_duration(p) for p in parts2) > 69
    assert m.split_audio_mp3(parts[0], t / "o3", False, chunk_seconds=30, segment_seconds=29) == [parts[0]]
    print("av ok (mp3 encode available)")
else:
    print("av ok (this PyAV build has no MP3 encoder: only already-mp3 audio can be added)")
