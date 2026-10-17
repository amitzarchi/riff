"""GStreamer audio engine: explicit channels, shared clock, no input monitoring."""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import gi

from .storage import PEAK_RATE, RATE, open_take
from .metronome import ClickSource, branch as metronome_branch

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)


@dataclass
class Device:
    name: str
    label: str
    channels: int
    backend: str = "pulse"


def devices() -> tuple[list[Device], list[Device]]:
    result = []
    for kind in ("sources", "sinks"):
        try:
            data = json.loads(subprocess.check_output(
                ["pactl", "-f", "json", "list", kind], timeout=4, stderr=subprocess.DEVNULL))
        except (OSError, subprocess.SubprocessError, ValueError):
            data = []
        found = []
        for item in data:
            if kind == "sources" and (item.get("monitor_of_sink") is not None or
                                       item["name"].endswith(".monitor")):
                continue
            match = re.search(r"(\d+)ch", item.get("sample_specification", ""))
            channel_map = item.get("channel_map", "").split(",")
            # pulsesrc cannot negotiate unpositioned AUX channels correctly.
            # Keep numbered USB inputs in their native order through PipeWire.
            backend = "pipewire" if kind == "sources" and all(
                channel.startswith("aux") for channel in channel_map) else "pulse"
            found.append(Device(item["name"], item.get("description", item["name"]),
                                int(match[1]) if match else 2, backend))
        result.append(found)
    # Some Quad Cortex / Linux combinations expose playback but omit the capture node.
    # Offer its hardware capture endpoint rather than silently recording a microphone.
    if not any("cortex" in d.name.lower() for d in result[0]):
        for stream in Path("/proc/asound").glob("card*/stream0"):
            try:
                text = stream.read_text()
                if "Quad Cortex" not in text or "Capture:" not in text:
                    continue
                card = (stream.parent / "id").read_text().strip()
                match = re.search(r"Channels:\s*(\d+)", text.split("Capture:", 1)[1])
                result[0].append(Device(f"hw:{card},0", "Quad Cortex · direct USB",
                                        int(match[1]) if match else 8, "alsa"))
            except OSError:
                continue
    for group in result:
        group.sort(key=lambda d: ("cortex" not in (d.name + d.label).lower(), d.label))
    return result[0], result[1]


def quote(value) -> str:
    return json.dumps(str(value))


def capture_source(device: Device) -> str:
    if device.backend == "pipewire":
        positions = "[ " + ", ".join(f"AUX{c}" for c in range(device.channels)) + " ]"
        # Gst boolean values serialize to "TRUE". The session manager needs the
        # lowercase string "true" for dont-remix to preserve all eight ports.
        properties = (f"props,audio.channels=(int){device.channels},audio.position=(string){quote(positions)},"
                      "stream.dont-remix=(string)true,node.dont-fallback=(string)true,"
                      "node.dont-reconnect=(string)true")
        return (f"pipewiresrc target-object={quote(device.name)} client-name=Riff on-disconnect=error "
                f"stream-properties={quote(properties)} ! "
                f"audio/x-raw,format=F32LE,rate={RATE},channels={device.channels},"
                "layout=interleaved,channel-mask=(bitmask)0x0")
    return (f"{device.backend}src device={quote(device.name)} "
            "buffer-time=40000 latency-time=10000")


def matrix(in_channels: int, selected: tuple[int, int]) -> str:
    rows = ["< " + ", ".join("1.0" if c == selected[r] else "0.0" for c in range(in_channels))
            + " >" for r in range(2)]
    return "< " + ", ".join(rows) + " >"


def output_chain(device: Device | None, test_sink: str | None = None) -> str:
    if test_sink:
        return test_sink
    # Send stereo only to USB 1/2. Never upmix into the QC's reamping channels.
    mapping = ""
    if device and device.channels > 2:
        rows = ["< " + ", ".join("1.0" if c == r else "0.0" for c in range(2)) + " >"
                for r in range(device.channels)]
        mapping = (f"audiomixmatrix in-channels=2 out-channels={device.channels} channel-mask=0 "
                   f"matrix=\"< {', '.join(rows)} >\" ! "
                   f"audio/x-raw,channels={device.channels},channel-mask=(bitmask)0x0 ! ")
    sink = "pulsesink client-name=Riff buffer-time=40000 latency-time=10000"
    if device:
        sink += f" device={quote(device.name)}"
    return mapping + sink


class Engine:
    def __init__(self, on_error=lambda message: None, on_end=lambda: None):
        self.on_error, self.on_end = on_error, on_end
        self.pipeline = None
        self.bus = None
        self.mode = "idle"
        self.writer = None
        self._take_file = None
        self.record_start = 0.0
        self.frames = 0
        self.peaks = []
        self.peak_db = -90.0
        self.record_limit = 60 * 60  # Standard WAV stays well below its 4 GB limit.
        self._lock = threading.Lock()
        self._first_buffer = True
        self.click_source = None

    def _launch(self, description: str, mode: str, configure=None, position=0.0):
        self.pipeline = Gst.parse_launch(description)
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._message)
        self.mode = mode
        capture = self.pipeline.get_by_name("capture")
        if capture:
            capture.connect("new-sample", self._sample)
        if configure:
            configure(self.pipeline)
        if position > 0:
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pipeline.get_state(2 * Gst.SECOND)
            if not self.seek(position):
                raise RuntimeError("Could not seek to the selected position.")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise RuntimeError("Could not start the audio device. Refresh devices in Audio settings.")

    def capture(self, device: Device, pair: tuple[int, int], path: Path | None = None,
                backing: Path | None = None, backing_gain=0.65,
                output: Device | None = None, source_override=None, test_sink=None, metronome=None, position=0.0):
        self.stop()
        self.frames, self.peaks, self._first_buffer = 0, [], True
        self.record_start = round(position * RATE) / RATE if path else 0.0
        if max(pair) >= device.channels:
            raise ValueError("The selected input channels are unavailable on this device.")
        if path:
            if not 0 <= position < self.record_limit:
                raise ValueError("Choose a recording start within the first hour.")
            self.writer, self._take_file = open_take(path, round(self.record_start * RATE))
            self.peaks = [0.0] * round(self.record_start * PEAK_RATE)
        source = source_override or capture_source(device)
        description = (
            f"{source} ! audioconvert ! audio/x-raw,format=S32LE,rate={RATE},"
            f"channels={device.channels},layout=interleaved ! "
            f"audiomixmatrix in-channels={device.channels} out-channels=2 channel-mask=3 "
            f"matrix=\"{matrix(device.channels, pair)}\" ! "
            "level name=input_level interval=20000000 post-messages=true ! audioconvert ! "
            f"audio/x-raw,format=S24LE,rate={RATE},channels=2 ! "
            "appsink name=capture emit-signals=true sync=false async=false "
            "max-buffers=8 drop=false wait-on-eos=false"
        )
        monitor_metronome = metronome is not None and (output is not None or test_sink is not None)
        if backing or monitor_metronome:
            description += (" audiomixer name=mix ! audioconvert ! "
                            f"audio/x-raw,format=F32LE,channels=2,rate={RATE} ! "
                            + output_chain(output, test_sink))
        if backing:
            description += (f" filesrc location={quote(backing)} ! wavparse ! audioconvert ! "
                            f"audio/x-raw,format=F32LE,channels=2,rate={RATE} ! "
                            f"volume name=backing_volume volume={backing_gain} ! queue ! mix.sink_0")
        if monitor_metronome:
            description += " " + metronome_branch(metronome)
        try:
            def configure(pipeline):
                if backing and position:
                    pipeline.get_by_name("mix").get_static_pad("sink_0").set_offset(-round(position * Gst.SECOND))
                if monitor_metronome:
                    self.click_source = ClickSource(metronome.bpm, self.record_limit - position,
                                                    metronome.alignment_ms, position=position)
                    self.click_source.attach(pipeline.get_by_name("metronome_source"))

            self._launch(description, "record" if path else "meter", configure)
        except Exception:
            self.stop()
            raise

    def _sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buffer = sample.get_buffer()
        if self.writer:
            raw = buffer.extract_dup(0, buffer.get_size())
            with self._lock:
                if self.writer:
                    try:
                        if self._first_buffer:
                            # Preserve the source timestamp so both files share timeline zero.
                            pts = buffer.pts if buffer.pts != Gst.CLOCK_TIME_NONE else 0
                            padding = min(int(pts * RATE / Gst.SECOND), RATE * 2)
                            self.writer.writeframes(b"\0" * (padding * 6))
                            self.frames += padding
                            self._first_buffer = False
                        self.writer.writeframes(raw)
                        self.frames += len(raw) // 6
                    except OSError as error:
                        GLib.idle_add(self.on_error, f"Could not save the recording: {error}")
                        return Gst.FlowReturn.ERROR
        return Gst.FlowReturn.OK

    def _message(self, bus, message):
        if bus != self.bus:
            return
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.on_error(error.message)
        elif message.type == Gst.MessageType.EOS:
            self.on_end()
        elif message.type == Gst.MessageType.LATENCY:
            if self.pipeline:
                self.pipeline.recalculate_latency()
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure and structure.get_name() == "level":
                values = structure.get_value("peak")
                self.peak_db = max(values) if values else -90
                if self.mode == "record":
                    self.peaks.append(10 ** (max(-90, self.peak_db) / 20))

    def play(self, backing: Path | None, take: Path | None, output: Device | None,
             backing_gain=0.65, guitar_gain=1.0, position=0.0, alignment_ms=0,
             test_sink=None, metronome=None):
        self.stop()
        if not backing and not take and not (metronome and metronome.enabled):
            return
        branches = []
        for index, (path, gain) in enumerate(((backing, backing_gain), (take, guitar_gain))):
            if path:
                branches.append(f"filesrc location={quote(path)} ! wavparse ! audioconvert ! "
                                f"audio/x-raw,format=F32LE,channels=2,rate={RATE} ! "
                                f"volume name={'backing' if index == 0 else 'guitar'}_volume volume={gain} ! "
                                f"queue ! mix.sink_{index}")
        if metronome is not None:
            branches.append(metronome_branch(metronome))
        duration = None
        if metronome is not None and (backing or take):
            durations = []
            for path, offset in ((backing, 0), (take, alignment_ms / 1000)):
                if path:
                    with wave.open(str(path)) as audio:
                        durations.append(max(0, audio.getnframes() / audio.getframerate() + offset))
            duration = max(durations)
        description = ("audiomixer name=mix ! audioconvert ! "
                       f"audio/x-raw,format=F32LE,channels=2,rate={RATE} ! "
                       + output_chain(output, test_sink) + " " + " ".join(branches))
        try:
            def configure(pipeline):
                if take and alignment_ms:
                    pipeline.get_by_name("mix").get_static_pad("sink_1").set_offset(alignment_ms * Gst.MSECOND)
                if metronome is not None:
                    self.click_source = ClickSource(metronome.bpm, duration, metronome.alignment_ms)
                    self.click_source.attach(pipeline.get_by_name("metronome_source"))

            self._launch(description, "play", configure, position)
        except Exception:
            self.stop()
            raise

    def set_gain(self, name: str, value: float):
        element = self.pipeline.get_by_name(f"{name}_volume") if self.pipeline else None
        if element:
            element.set_property("volume", value)

    def set_metronome(self, settings):
        if self.click_source:
            self.click_source.bpm = settings.bpm
            self.click_source.alignment_ms = settings.alignment_ms
        element = self.pipeline.get_by_name("metronome_volume") if self.pipeline else None
        if element:
            element.set_property("volume", settings.gain)
            element.set_property("mute", not settings.enabled)

    def seek(self, seconds: float):
        if self.pipeline and self.mode in ("play", "pause"):
            return self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                             int(max(0, seconds) * Gst.SECOND))
        return False

    def pause(self):
        if self.pipeline and self.mode in ("play", "pause"):
            self.mode = "pause" if self.mode == "play" else "play"
            self.pipeline.set_state(Gst.State.PAUSED if self.mode == "pause" else Gst.State.PLAYING)

    @property
    def position(self) -> float:
        if self.mode == "record":
            return self.record_start + self.frames / RATE
        if self.pipeline and self.mode in ("play", "pause"):
            success, position = self.pipeline.query_position(Gst.Format.TIME)
            return position / Gst.SECOND if success else 0.0
        return 0.0

    def stop(self):
        pipeline, self.pipeline = self.pipeline, None
        if self.bus:
            self.bus.remove_signal_watch()
            self.bus = None
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
        self.click_source = None
        with self._lock:
            try:
                if self.writer:
                    self.writer.close()
            finally:
                self.writer = None
                if self._take_file:
                    self._take_file.close()
                    self._take_file = None
        self.mode = "idle"
        self.peak_db = -90


def amplitude(db: float) -> float:
    return min(1, max(0, (db + 60) / 60)) if math.isfinite(db) else 0
