"""Live Music playback: oscillators are mixed block by block as the song plays and
streamed straight to the device's audio output. Nothing is rendered to a file.

  desktop : sounddevice (PortAudio) output stream
  Android : android.media.AudioTrack in streaming mode, through pyjnius
"""
from __future__ import annotations

import threading
import time
from bisect import bisect_left
from typing import Callable, Optional

import numpy as np

import synth

SAMPLE_RATE = synth.SAMPLE_RATE
BLOCK = 1024                     # samples per mixed block (~46 ms)
TAIL_SECONDS = 1.5


class AudioUnavailable(RuntimeError):
    pass


# ── output sinks ───────────────────────────────────────────────────────────

class Sink:
    def open(self, sr: int) -> None: ...
    def write(self, pcm: bytes) -> None: ...      # blocks until queued
    def pause(self) -> None: ...                  # stop sound and drop queued audio
    def resume(self) -> None: ...
    def flush(self) -> None: ...                  # drop queued audio, keep state
    def close(self) -> None: ...


class DesktopSink(Sink):
    def __init__(self):
        try:
            import sounddevice as sd
        except Exception as ex:
            raise AudioUnavailable(f"Audio output isn't available ({ex}).")
        self.sd = sd
        self.stream = None

    def open(self, sr: int) -> None:
        try:
            self.stream = self.sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16")
            self.stream.start()
        except Exception as ex:
            raise AudioUnavailable(f"Couldn't open the audio device ({ex}).")

    def write(self, pcm: bytes) -> None:
        self.stream.write(pcm)

    def pause(self) -> None:
        self.stream.abort()

    def resume(self) -> None:
        self.stream.start()

    def flush(self) -> None:
        self.stream.abort()
        self.stream.start()

    def close(self) -> None:
        if self.stream is not None:
            try:
                self.stream.abort()
                self.stream.close()
            except Exception:
                pass
            self.stream = None


class AndroidSink(Sink):
    def __init__(self):
        try:
            from jnius import autoclass
        except Exception as ex:
            raise AudioUnavailable(f"Audio output isn't available ({ex}).")
        self.AudioTrack = autoclass("android.media.AudioTrack")
        self.AudioManager = autoclass("android.media.AudioManager")
        self.AudioFormat = autoclass("android.media.AudioFormat")
        self.track = None

    def open(self, sr: int) -> None:
        AF, AT = self.AudioFormat, self.AudioTrack
        minbuf = AT.getMinBufferSize(sr, AF.CHANNEL_OUT_MONO, AF.ENCODING_PCM_16BIT)
        if minbuf <= 0:
            raise AudioUnavailable("Couldn't open the audio device.")
        self.track = AT(self.AudioManager.STREAM_MUSIC, sr, AF.CHANNEL_OUT_MONO,
                        AF.ENCODING_PCM_16BIT, max(minbuf * 4, BLOCK * 2 * 4), AT.MODE_STREAM)
        self.track.play()

    def write(self, pcm: bytes) -> None:
        try:
            self.track.write(pcm, 0, len(pcm))
        except Exception:
            import array
            shorts = array.array("h")
            shorts.frombytes(pcm)
            self.track.write(shorts.tolist(), 0, len(shorts))

    def pause(self) -> None:
        self.track.pause()
        self.track.flush()

    def resume(self) -> None:
        self.track.play()

    def flush(self) -> None:
        self.track.pause()
        self.track.flush()
        self.track.play()

    def close(self) -> None:
        if self.track is not None:
            try:
                self.track.stop()
                self.track.release()
            except Exception:
                pass
            self.track = None


class ListSink(Sink):
    """Collects PCM in memory (tests / no audio device). `realtime` paces writes."""
    def __init__(self, realtime: bool = False):
        self.chunks: list[bytes] = []
        self.realtime = realtime
        self.opened = self.closed = False

    def open(self, sr: int) -> None:
        self.sr, self.opened = sr, True

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)
        if self.realtime:
            time.sleep(len(pcm) / 2 / self.sr)

    def close(self) -> None:
        self.closed = True


def make_sink(android: bool) -> Sink:
    return AndroidSink() if android else DesktopSink()


# ── engine ─────────────────────────────────────────────────────────────────

class LiveSynth:
    def __init__(self, music: dict, bpm: float, sink_factory: Callable[[], Sink],
                 on_end: Optional[Callable[[], None]] = None, sr: int = SAMPLE_RATE):
        self.sr = sr
        self.on_end = on_end
        self.sink_factory = sink_factory
        self.voices, self.warnings = synth.voices_from_music(music)
        notes = [n for n in synth.collect_notes(music, bpm)
                 if n["track"] < len(self.voices) and not self.voices[n["track"]]["muted"]]
        self.notes = notes
        self.starts = [int(n["t"] * sr) for n in notes]
        self._bufs: dict = {}
        self._max_len = max((len(self._buf(n)) for n in notes[:1]), default=0)
        for n in notes:
            self._max_len = max(self._max_len, len(self._buf(n)))
        last = (self.starts[-1] + self._max_len) if notes else 0
        self.total = last + int(TAIL_SECONDS * sr) if notes else 0
        self.pos = 0
        self.loop = True
        self._cond = threading.Condition()
        self._playing = False
        self._closed = False
        self._seek: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    # state ---------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.total / self.sr

    @property
    def position(self) -> float:
        return min(self.pos, self.total) / self.sr

    @property
    def playing(self) -> bool:
        return self._playing

    def _buf(self, note: dict) -> np.ndarray:
        key = (note["track"], note["key"])
        if key not in self._bufs:
            self._bufs[key] = synth.note_buffer(self.voices[note["track"]], note["key"], self.sr)
        return self._bufs[key]

    # control -------------------------------------------------------------
    def play(self) -> None:
        with self._cond:
            if self._closed or not self.notes:
                return
            if self.pos >= self.total:
                self.pos = 0
            self._playing = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def pause(self) -> None:
        with self._cond:
            self._playing = False

    def stop(self) -> None:
        with self._cond:
            self._playing = False
            self._seek = 0
            self.pos = 0

    def seek(self, seconds: float) -> None:
        with self._cond:
            self._seek = max(0, min(int(seconds * self.sr), max(0, self.total - 1)))
            self.pos = self._seek

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._playing = False
            self._cond.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2)

    # mixing --------------------------------------------------------------
    def render(self, pos: int, n: int) -> np.ndarray:
        mix = np.zeros(n, dtype=np.float32)
        lo = bisect_left(self.starts, pos - self._max_len)
        hi = bisect_left(self.starts, pos + n)
        for i in range(lo, hi):
            note = self.notes[i]
            buf = self._buf(note)
            off = self.starts[i] - pos
            s0 = max(0, -off)
            d0 = max(0, off)
            length = min(len(buf) - s0, n - d0)
            if length > 0:
                mix[d0:d0 + length] += buf[s0:s0 + length] * (note["vel"] / 127.0)
        return np.tanh(mix * 0.9)            # soft limiter

    def _run(self) -> None:
        try:
            sink = self.sink_factory()
            sink.open(self.sr)
        except Exception as ex:
            self.error = str(ex)
            self._playing = False
            return
        sink_paused = False
        try:
            while True:
                with self._cond:
                    while not self._playing and not self._closed:
                        if not sink_paused:
                            sink.pause()
                            sink_paused = True
                        self._cond.wait(0.2)
                    if self._closed:
                        return
                    if sink_paused:
                        sink.resume()
                        sink_paused = False
                    if self._seek is not None:
                        sink.flush()
                        self._seek = None
                    pos = self.pos
                if pos >= self.total:
                    if self.loop:
                        with self._cond:
                            self.pos = 0
                        continue
                    with self._cond:
                        self._playing = False
                        self.pos = self.total
                    if self.on_end:
                        self.on_end()
                    continue
                block = self.render(pos, BLOCK)
                pcm = (block * 32767).astype("<i2").tobytes()
                sink.write(pcm)
                with self._cond:
                    if self._seek is None and self._playing:
                        self.pos = pos + BLOCK
        except Exception as ex:
            self.error = str(ex)
            self._playing = False
        finally:
            sink.close()
