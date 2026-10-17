"""Camera discovery, a native GTK preview, and shared-clock WebM recording."""

from dataclasses import dataclass
import json
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)

WIDTH, HEIGHT, FPS = 1280, 720, 30


@dataclass(frozen=True)
class Camera:
    name: str
    label: str


@dataclass(frozen=True)
class Recording:
    camera: Camera
    path: Path
    guitar_gain: float = 1.0
    alignment_ms: int = 0
    source_override: str | None = None
    preview_sink: str | None = None

    @property
    def temporary(self):
        return self.path.with_suffix(self.path.suffix + ".part")


def cameras():
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Video/Source", Gst.Caps.from_string("video/x-raw;image/jpeg"))
    found = {}
    try:
        if not monitor.start():
            return []
        links = list(Path("/dev/v4l/by-id").glob("*"))
        for device in monitor.get_devices():
            properties = device.get_properties()
            if not properties:
                continue
            path = properties.get_string("device.path") or properties.get_string("api.v4l2.path")
            if not path:
                continue
            name = next((str(link) for link in links if link.resolve() == Path(path).resolve()), path)
            label = f"{device.get_display_name()} · {Path(path).name}"
            default = bool(properties.get_value("is-default")) if properties.has_field("is-default") else False
            found[name] = (not default, Camera(name, label))
    finally:
        monitor.stop()
    return [camera for _, camera in sorted(found.values(), key=lambda item: (item[0], item[1].label))]


def require_elements(*names):
    missing = [name for name in names if not Gst.ElementFactory.find(name)]
    if missing:
        raise RuntimeError("Missing camera support: " + ", ".join(missing) +
                           ". Install gst-plugin-gtk4 and gst-plugins-good.")


def source(camera, override=None):
    capture = override or f"v4l2src device={json.dumps(camera.name)} do-timestamp=true"
    return (f"{capture} ! decodebin ! videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,format=I420,width={WIDTH},height={HEIGHT},framerate={FPS}/1,pixel-aspect-ratio=1/1")


def preview_sink(override=None, sync=False):
    if override:
        return override
    require_elements("gtk4paintablesink")
    return (f"gtk4paintablesink name=camera_preview sync={'true' if sync else 'false'} "
            f"async={'true' if sync else 'false'}")


def paintable(pipeline):
    sink = pipeline.get_by_name("camera_preview") if pipeline else None
    return sink.get_property("paintable") if sink and sink.find_property("paintable") else None


def recording_branch(recording):
    require_elements("vp8enc", "opusenc", "webmmux")
    return (
        f"{source(recording.camera, recording.source_override)} ! tee name=camera_tee "
        "camera_tee. ! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        f"{preview_sink(recording.preview_sink)} "
        "camera_tee. ! queue ! vp8enc deadline=1 cpu-used=4 threads=2 target-bitrate=2500000 "
        f"keyframe-max-dist={FPS * 2} ! queue ! camera_mux.video_0 "
        "audiomixer name=video_mix ! audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=2 ! opusenc bitrate=192000 ! queue ! camera_mux.audio_0 "
        "webmmux name=camera_mux ! "
        f"filesink location={json.dumps(str(recording.temporary))} sync=false async=false"
    )


class Preview:
    def __init__(self, on_error=lambda message: None):
        self.on_error = on_error
        self.pipeline = self.bus = None

    def start(self, camera, source_override=None, test_sink=None):
        self.stop()
        try:
            description = f"{source(camera, source_override)} ! {preview_sink(test_sink)}"
            self.pipeline = Gst.parse_launch(description)
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect("message", self._message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Could not open this camera. Close other camera apps or select another input.")
            return paintable(self.pipeline)
        except Exception:
            self.stop()
            raise

    def _message(self, bus, message):
        if bus != self.bus:
            return
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.stop()
            self.on_error(error.message)
        elif message.type == Gst.MessageType.EOS:
            self.stop()
            self.on_error("The camera stopped. Refresh cameras to reconnect.")

    def stop(self):
        pipeline, self.pipeline = self.pipeline, None
        if self.bus:
            self.bus.remove_signal_watch()
            self.bus = None
        if pipeline:
            pipeline.set_state(Gst.State.NULL)


class Playback(Preview):
    """Play only the recorded picture, following the session audio transport."""

    def __init__(self, on_error=lambda message: None):
        super().__init__(on_error)
        self.playing, self.ended, self.duration = False, False, 0.0

    def start(self, path, clock=None, test_sink=None):
        self.stop()
        require_elements("matroskademux", "vp8dec")
        try:
            self.pipeline = Gst.parse_launch(
                f"filesrc location={json.dumps(str(path))} ! matroskademux name=take_demux "
                "take_demux.video_0 ! queue ! vp8dec ! videoconvert ! " + preview_sink(test_sink, sync=True) +
                " take_demux.audio_0 ! queue ! fakesink sync=false async=false")
            if clock:
                self.pipeline.use_clock(clock)
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect("message", self._message)
            self.pipeline.set_state(Gst.State.PAUSED)
            if self.pipeline.get_state(2 * Gst.SECOND)[0] == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Could not open the recorded take video.")
            success, duration = self.pipeline.query_duration(Gst.Format.TIME)
            self.duration = duration / Gst.SECOND if success else 0.0
            return paintable(self.pipeline)
        except Exception:
            self.stop()
            raise

    @property
    def position(self):
        if self.pipeline:
            sink = self.pipeline.get_by_name("camera_preview")
            success, position = sink.query_position(Gst.Format.TIME) if sink else (False, 0)
            return position / Gst.SECOND if success else 0.0
        return 0.0

    def _set_playing(self, playing):
        if self.playing != playing:
            self.pipeline.set_state(Gst.State.PLAYING if playing else Gst.State.PAUSED)
            self.playing = playing

    def sync(self, position, start, mode, force=False):
        if not self.pipeline:
            return None
        if not self.duration:
            success, duration = self.pipeline.query_duration(Gst.Format.TIME)
            self.duration = duration / Gst.SECOND if success else 0.0
        relative = position - start
        if relative < 0:
            self._set_playing(False)
            return "before"
        if self.duration and relative >= self.duration:
            self._set_playing(False)
            return "after"
        if force or self.ended or abs(self.position - relative) > 0.05:
            self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                                      round(max(0, relative) * Gst.SECOND))
            self.ended = False
        self._set_playing(mode == "play")
        return "playing" if mode == "play" else "paused"

    def _message(self, bus, message):
        if bus != self.bus:
            return
        if message.type == Gst.MessageType.EOS:
            self.ended = True
            self._set_playing(False)
        else:
            super()._message(bus, message)

    def stop(self):
        super().stop()
        self.playing, self.ended, self.duration = False, False, 0.0
