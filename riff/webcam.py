"""Camera discovery, a native GTK preview, and shared-clock WebM recording."""

from dataclasses import dataclass
import json
from pathlib import Path

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

Gst.init(None)


@dataclass(frozen=True)
class Quality:
    key: str
    label: str
    width: int
    height: int
    bitrate: int


BALANCED = Quality("balanced", "Balanced · 720p", 1280, 720, 2_500_000)
HIGH = Quality("high", "High · 1080p", 1920, 1080, 8_000_000)
QUALITIES = (BALANCED, HIGH)


@dataclass(frozen=True)
class CaptureMode:
    width: int
    height: int
    fps_num: int = 30
    fps_den: int = 1
    input_caps: str | None = None

    @property
    def description(self):
        size = {(1280, 720): "720p", (1920, 1080): "1080p"}.get(
            (self.width, self.height), f"{self.width}×{self.height}")
        return f"{size} / {self.fps_num / self.fps_den:g} fps"


@dataclass(frozen=True)
class Camera:
    name: str
    label: str
    caps: str | None = None


def capture_mode(camera, quality=BALANCED):
    """Choose a native mode, reducing size/rate when needed without upscaling."""
    if not camera.caps:
        # Older/unknown devices retain the existing, conservative capture path.
        return CaptureMode(BALANCED.width, BALANCED.height)
    caps = Gst.Caps.from_string(camera.caps)
    supported = caps.intersect(Gst.Caps.from_string("video/x-raw;image/jpeg"))
    modes = []
    for index in range(supported.get_size()):
        structure = supported.get_structure(index)
        structure.fixate_field_nearest_int("width", quality.width)
        structure.fixate_field_nearest_int("height", quality.height)
        structure.fixate_field_nearest_fraction("framerate", 30, 1)
        has_width, width = structure.get_int("width")
        has_height, height = structure.get_int("height")
        has_rate, numerator, denominator = structure.get_fraction("framerate")
        if not (has_width and has_height and has_rate and width > 1 and height > 1
                and numerator > 0 and denominator > 0):
            continue
        native_rate = numerator / denominator
        if native_rate > 30:
            numerator, denominator = 30, 1
        ratio = min(1, quality.width / width, quality.height / height)
        output_width, output_height = int(width * ratio) // 2 * 2, int(height * ratio) // 2 * 2
        fixed = Gst.Caps.new_empty()
        fixed.append_structure(structure)
        mode = CaptureMode(output_width, output_height, numerator, denominator, fixed.fixate().to_string())
        score = (native_rate >= 29, output_width * output_height, min(native_rate, 30),
                 -abs(native_rate - 30), -width * height)
        modes.append((score, mode))
    if not modes:
        raise RuntimeError("This camera has no usable video mode. Select another input or press Refresh.")
    return max(modes, key=lambda candidate: candidate[0])[1]


@dataclass(frozen=True)
class Recording:
    camera: Camera
    path: Path
    guitar_gain: float = 1.0
    alignment_ms: int = 0
    source_override: str | None = None
    preview_sink: str | None = None
    quality: Quality = BALANCED
    mirrored: bool = False

    @property
    def mode(self):
        return capture_mode(self.camera, self.quality)

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
            caps = device.get_caps()
            found[name] = (not default, Camera(name, label, caps.to_string() if caps else None))
    finally:
        monitor.stop()
    return [camera for _, camera in sorted(found.values(), key=lambda item: (item[0], item[1].label))]


def require_elements(*names):
    missing = [name for name in names if not Gst.ElementFactory.find(name)]
    if missing:
        raise RuntimeError("Missing camera support: " + ", ".join(missing) +
                           ". Install gst-plugin-gtk4 and gst-plugins-good.")


def source(camera, override=None, mode=None, mirrored=False):
    mode = mode or capture_mode(camera)
    capture = override or f"v4l2src device={json.dumps(camera.name)} do-timestamp=true"
    if mode.input_caps:
        capture += f" ! capsfilter caps={json.dumps(mode.input_caps)}"
    flip = ""
    if mirrored:
        require_elements("videoflip")
        flip = " ! videoflip video-direction=horiz"
    return (f"{capture} ! decodebin ! videoconvert ! videoscale ! videorate ! "
            f"video/x-raw,format=I420,width={mode.width},height={mode.height},"
            f"framerate={mode.fps_num}/{mode.fps_den},pixel-aspect-ratio=1/1{flip}")


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
    mode = recording.mode
    threads = 4 if recording.quality == HIGH else 2
    return (
        f"{source(recording.camera, recording.source_override, mode, recording.mirrored)} ! tee name=camera_tee "
        "camera_tee. ! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! "
        f"{preview_sink(recording.preview_sink)} "
        f"camera_tee. ! queue ! vp8enc deadline=1 cpu-used=4 threads={threads} "
        f"target-bitrate={recording.quality.bitrate} keyframe-max-dist={round(mode.fps_num / mode.fps_den * 2)} "
        "! queue ! camera_mux.video_0 "
        "audiomixer name=video_mix ! audioconvert ! audioresample ! "
        "audio/x-raw,format=S16LE,rate=48000,channels=2 ! opusenc bitrate=192000 ! queue ! camera_mux.audio_0 "
        "webmmux name=camera_mux ! "
        f"filesink location={json.dumps(str(recording.temporary))} sync=false async=false"
    )


class Preview:
    def __init__(self, on_error=lambda message: None):
        self.on_error = on_error
        self.pipeline = self.bus = None
        self.mode = None

    def start(self, camera, source_override=None, test_sink=None, quality=BALANCED, mirrored=False):
        self.stop()
        try:
            self.mode = capture_mode(camera, quality)
            description = f"{source(camera, source_override, self.mode, mirrored)} ! {preview_sink(test_sink)}"
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
