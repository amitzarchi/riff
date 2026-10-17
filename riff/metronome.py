"""A seekable click source for monitoring. It never feeds the recording writer."""

from array import array
from collections import deque
from dataclasses import dataclass
import math
import sys
import threading
import time

import gi

from .storage import RATE

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

ALIGNMENT_LIMIT_MS = 60_000
MAX_GAIN = 2.0


class TapTempo:
    """Average the last four beat intervals; pause for three seconds to restart."""

    def __init__(self):
        self._taps = deque(maxlen=5)

    def reset(self):
        self._taps.clear()

    def tap(self, timestamp=None):
        timestamp = time.monotonic() if timestamp is None else timestamp
        if self._taps:
            gap = timestamp - self._taps[-1]
            if gap >= 3:
                self.reset()
            elif gap < 0.1:
                return None  # Ignore accidental double clicks.
        self._taps.append(timestamp)
        if len(self._taps) < 2:
            return None
        interval = (self._taps[-1] - self._taps[0]) / (len(self._taps) - 1)
        return max(40, min(300, round(60 / interval)))


@dataclass(frozen=True)
class MetronomeSettings:
    enabled: bool = False
    bpm: int = 120
    gain: float = 0.3
    alignment_ms: int = 0

    @classmethod
    def from_session(cls, data):
        return cls(bool(data.get("metronome_enabled", False)),
                   max(40, min(300, int(data.get("metronome_bpm", 120)))),
                   max(0.0, min(MAX_GAIN, float(data.get("metronome_gain", 0.3)))),
                   max(-ALIGNMENT_LIMIT_MS, min(ALIGNMENT_LIMIT_MS,
                       int(data.get("metronome_alignment_ms", 0)))))


def _click(frequency):
    samples = array("f")
    for frame in range(round(RATE * 0.018)):
        time = frame / RATE
        envelope = 0.5 * min(1, time / 0.0005) * math.exp(-time / 0.0035)
        value = envelope * math.sin(2 * math.pi * frequency * time)
        samples.extend((value, value))
    return samples


class ClickSource:
    def __init__(self, bpm=120, duration=None, alignment_ms=0, position=0.0):
        self.bpm = bpm
        self.alignment_ms = alignment_ms
        self.frame = 0
        self.origin = round(position * RATE)
        self.end = round(duration * RATE) if duration is not None else None
        self._clicks = (_click(1100), _click(1760))
        self._lock = threading.Lock()

    def render(self, start, frames):
        """Render by absolute sample index, avoiding cumulative tempo drift."""
        samples = array("f", [0.0]) * (frames * 2)
        period = RATE * 60 / self.bpm
        offset = round(self.alignment_ms * RATE / 1000)
        click_frames = len(self._clicks[0]) // 2
        first = max(0, math.floor((start - offset - click_frames) / period))
        last = math.ceil((start + frames - offset) / period)
        for beat in range(first, last + 1):
            position = offset + round(beat * period)
            left, right = max(start, position), min(start + frames, position + click_frames)
            if left < right:
                click = self._clicks[beat % 4 == 0]
                samples[(left - start) * 2:(right - start) * 2] = click[(left - position) * 2:(right - position) * 2]
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()

    def attach(self, source):
        if self.end is not None:
            source.set_property("duration", Gst.util_uint64_scale(self.end, Gst.SECOND, RATE))
        source.connect("need-data", self._need_data)
        source.connect("seek-data", self._seek)

    def _seek(self, _source, offset):
        with self._lock:
            self.frame = round(offset * RATE / Gst.SECOND)
        return True

    def _need_data(self, source, _length):
        with self._lock:
            start = self.frame
            count = 1024 if self.end is None else min(1024, self.end - start)
            if count > 0:
                raw = self.render(start + self.origin, count)
                self.frame += count
        if count <= 0:
            source.emit("end-of-stream")
            return
        buffer = Gst.Buffer.new_wrapped(raw)
        buffer.pts = Gst.util_uint64_scale(start, Gst.SECOND, RATE)
        buffer.duration = Gst.util_uint64_scale(start + count, Gst.SECOND, RATE) - buffer.pts
        buffer.offset, buffer.offset_end = start, start + count
        source.emit("push-buffer", buffer)


def branch(settings):
    return (f"appsrc name=metronome_source format=time stream-type=seekable is-live=false "
            f"block=true max-bytes=16384 caps=\"audio/x-raw,format=F32LE,rate={RATE},channels=2,"
            "layout=interleaved,channel-mask=(bitmask)0x3\" ! "
            "queue max-size-time=40000000 max-size-buffers=0 max-size-bytes=0 ! "
            f"volume name=metronome_volume volume={settings.gain} mute={'false' if settings.enabled else 'true'} ! "
            "mix.sink_2")
