"""FFmpeg without the ffmpeg binary: PyAV replacements for castletool's
ffmpeg/ffprobe subprocess calls.

Phones can't ship or spawn an ffmpeg executable, but PyAV (`av`, with Flet's
`flet-libffmpeg` on Android) exposes the same libraries to Python. `install()`
swaps these in for the matching functions in castletool_core, so every code path
that used ffmpeg (video frames, sound extraction, mp3 splitting) now uses them.

MP3 *encoding* needs libmp3lame, which the Android PyAV build may not include.
Already-mp3 sources are split without re-encoding, so they always work.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

try:
    import av
    HAS_AV = True
except Exception:  # not installed / failed to load native libs
    av = None
    HAS_AV = False

SAMPLE_RATE = 44100


def can_encode_mp3() -> bool:
    if not HAS_AV:
        return False
    try:
        av.codec.Codec("libmp3lame", "w")
        return True
    except Exception:
        return False


# ── video ────────────────────────────────────────────────────────────────

def probe_video_dimensions(path: Path) -> tuple[int, int]:
    try:
        with av.open(str(path)) as c:
            vs = c.streams.video[0]
            return int(vs.codec_context.width), int(vs.codec_context.height)
    except Exception:
        return 64, 64


def extract_mp4_frames(path: Path, width: int, height: int, every_n: int = 1,
                       smooth: bool = False) -> tuple[list[bytes], float]:
    """Every Nth frame as PNG bytes scaled to width x height, plus effective fps."""
    from PIL import Image
    every_n = max(1, int(every_n))
    frames: list[bytes] = []
    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        rate = vs.average_rate or vs.guessed_rate
        fps = float(rate) if rate else 30.0
        resample = Image.BILINEAR if smooth else Image.NEAREST
        for i, frame in enumerate(c.decode(vs)):
            if i % every_n:
                continue
            img = frame.to_image().convert("RGB")
            if img.size != (width, height):
                img = img.resize((width, height), resample)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            frames.append(buf.getvalue())
    if not frames:
        raise SystemExit("No frames could be read from the video.")
    return frames, fps / every_n


# ── audio ────────────────────────────────────────────────────────────────

def probe_has_audio(path: Path) -> bool:
    try:
        with av.open(str(path)) as c:
            return len(c.streams.audio) > 0
    except Exception:
        return False


def probe_duration(path: Path) -> Optional[float]:
    try:
        with av.open(str(path)) as c:
            if c.duration:
                return c.duration / av.time_base
            s = c.streams.audio[0]
            if s.duration and s.time_base:
                return float(s.duration * s.time_base)
    except Exception:
        pass
    return None


def _decode_pcm(path: Path):
    """Whole soundtrack as int16 stereo 44.1 kHz, shape (2, n)."""
    import numpy as np
    chunks = []
    with av.open(str(path)) as c:
        if not c.streams.audio:
            raise RuntimeError("The file has no sound track.")
        rs = av.AudioResampler(format="s16p", layout="stereo", rate=SAMPLE_RATE)
        for frame in c.decode(c.streams.audio[0]):
            for out in rs.resample(frame):
                chunks.append(out.to_ndarray())
        for out in rs.resample(None):
            chunks.append(out.to_ndarray())
    if not chunks:
        raise RuntimeError("The file has no sound track.")
    return np.concatenate(chunks, axis=1)


def _encode_mp3(pcm, out_path: Path, bitrate: int = 96000) -> None:
    import numpy as np
    out = av.open(str(out_path), "w", format="mp3")
    st = out.add_stream("libmp3lame", rate=SAMPLE_RATE)
    st.bit_rate = bitrate
    st.layout = "stereo"
    n, step, pos = pcm.shape[1], 1152, 0
    while pos < n:
        block = np.ascontiguousarray(pcm[:, pos:pos + step])
        frame = av.AudioFrame.from_ndarray(block, format="s16p", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = pos
        for pkt in st.encode(frame):
            out.mux(pkt)
        pos += step
    for pkt in st.encode(None):
        out.mux(pkt)
    out.close()


def _remux_segments(path: Path, out_dir: Path, stem: str, seg_seconds: float) -> list[Path]:
    """Split an existing mp3 on packet boundaries, no re-encoding."""
    parts: list[Path] = []
    inp = av.open(str(path))
    ins = inp.streams.audio[0]
    out = ost = None
    cur, base = -1, 0
    for pkt in inp.demux(ins):
        if pkt.pts is None or pkt.size == 0:
            continue
        idx = int(float(pkt.pts * ins.time_base) // seg_seconds)
        if idx != cur:
            if out is not None:
                out.close()
            cur, base = idx, pkt.pts
            p = out_dir / f"{stem}_part{len(parts):03d}.mp3"
            parts.append(p)
            out = av.open(str(p), "w", format="mp3")
            ost = out.add_stream_from_template(ins)
        pkt.pts -= base
        pkt.dts = pkt.pts
        pkt.stream = ost
        out.mux(pkt)
    if out is not None:
        out.close()
    inp.close()
    return parts


def split_audio_mp3(path: Path, out_dir: Path, is_video: bool, *, chunk_seconds: float,
                    segment_seconds: float) -> list[Path]:
    """Sound of `path` as mp3 part(s) of at most chunk_seconds, in playback order."""
    already_mp3 = (not is_video) and path.suffix.lower() == ".mp3"
    duration = probe_duration(path)
    if already_mp3 and duration is not None and duration <= chunk_seconds:
        return [path]
    stem = "audio" if is_video else path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    if already_mp3:
        parts = _remux_segments(path, out_dir, stem, segment_seconds)
    else:
        if not can_encode_mp3():
            raise RuntimeError("This device can't encode MP3, so only .mp3 files can be added.")
        pcm = _decode_pcm(path)
        seg = int(segment_seconds * SAMPLE_RATE)
        parts = []
        for i, start in enumerate(range(0, pcm.shape[1], seg)):
            p = out_dir / f"{stem}_part{i:03d}.mp3"
            _encode_mp3(pcm[:, start:start + seg], p)
            parts.append(p)
    if not parts:
        raise RuntimeError("No audio was produced. Does the file have a sound track?")
    if len(parts) == 1:
        single = out_dir / f"{stem}.mp3"
        parts[0].replace(single)
        return [single]
    return parts


def extract_audio_mp3(path: Path, out_path: Path) -> None:
    if not can_encode_mp3():
        raise RuntimeError("This device can't encode MP3.")
    _encode_mp3(_decode_pcm(path), Path(out_path))


# ── wiring ───────────────────────────────────────────────────────────────

def install(core) -> None:
    """Replace castletool_core's ffmpeg-subprocess helpers with the PyAV ones."""
    core.HAS_FFMPEG = HAS_AV      # name kept; means "can read/convert media"
    if not HAS_AV:
        return
    core.probe_video_dimensions = probe_video_dimensions
    core.extract_mp4_frames = extract_mp4_frames
    core.probe_has_audio = probe_has_audio
    core.probe_duration = probe_duration
    core.extract_audio_mp3 = extract_audio_mp3
    core.split_audio_mp3 = lambda path, out_dir, is_video: split_audio_mp3(
        path, out_dir, is_video,
        chunk_seconds=core.AUDIO_CHUNK_SECONDS, segment_seconds=core.AUDIO_SEGMENT_SECONDS)
