from __future__ import annotations

import math
import signal
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import theme  # noqa: E402
from .audio import Engine, amplitude, devices  # noqa: E402
from .download import DownloadCancelled, YouTubeDownload  # noqa: E402
from .metronome import ALIGNMENT_LIMIT_MS, MetronomeSettings, TapTempo  # noqa: E402
from .storage import (PEAK_RATE, STATE_DIR, Session, atomic_json, export_audio,  # noqa: E402
                      import_backing, read_json, read_peaks)


def label(text, classes=(), xalign=0):
    widget = Gtk.Label(label=text, xalign=xalign)
    for name in classes:
        widget.add_css_class(name)
    return widget


def button(text=None, icon=None, callback=None, classes=(), tooltip=None):
    widget = Gtk.Button(label=text) if text else Gtk.Button(icon_name=icon)
    for name in classes:
        widget.add_css_class(name)
    if callback:
        widget.connect("clicked", lambda *_: callback())
    if tooltip:
        widget.set_tooltip_text(tooltip)
    return widget


def box(vertical=False, spacing=0, children=()):
    widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,
                     spacing=spacing)
    for child in children:
        widget.append(child)
    return widget


def margins(widget, value):
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, "set_margin_" + side)(value)
    return widget


def timecode(seconds, decimals=False):
    seconds = max(0, seconds)
    minutes, remaining = divmod(seconds, 60)
    return f"{int(minutes):02}:{remaining:04.1f}" if decimals else f"{int(minutes):02}:{int(remaining):02}"


class Waveform(Gtk.DrawingArea):
    def __init__(self, window, color, placeholder):
        super().__init__(hexpand=True, vexpand=True)
        self.window, self.color, self.placeholder = window, color, placeholder
        self.peaks = []
        self.duration = 0
        self.set_content_height(138)
        self.set_draw_func(self.draw)
        gesture = Gtk.GestureClick()
        gesture.connect("pressed", self.clicked)
        self.add_controller(gesture)
        self.set_cursor_from_name("crosshair")
        self.set_tooltip_text("Click to seek · recordings always start at the beginning")

    def clicked(self, _gesture, _n, x, _y):
        self.window.seek(max(0, min(1, (x - 20) / max(1, self.get_width() - 40))) * self.window.duration)

    def draw(self, _area, cr, width, height):
        def color(hex_color, alpha=1):
            cr.set_source_rgba(*(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)), alpha)

        c = self.window.colors
        left, right, center = 20, width - 20, height / 2
        span = max(1, right - left)
        total = self.window.duration
        color(c["border"], 0.7)
        cr.set_line_width(1)
        cr.move_to(left, center)
        cr.line_to(right, center)
        cr.stroke()
        step = next((s for s in (5, 10, 15, 30, 60, 120, 300, 600, 1800) if total / s <= span / 75), 3600)
        cr.select_font_face("monospace", 0, 0)
        cr.set_font_size(9)
        for tick in range(0, int(total) + 1, step):
            x = left + tick / total * span
            color(c["border"], 0.6)
            cr.move_to(x + 0.5, 26)
            cr.line_to(x + 0.5, height - 15)
            cr.stroke()
            color(c["muted_text"])
            cr.move_to(x + 3, 17)
            cr.show_text(timecode(tick))
        if self.peaks:
            color(c[self.color], 0.9)
            # Reduce actual envelopes into pixel columns; no fabricated waveform.
            count = min(int(span / 3), max(1, math.ceil(self.duration / total * span / 3)))
            for index in range(count):
                start = int(index * total / span * 3 * PEAK_RATE)
                end = max(start + 1, int((index + 1) * total / span * 3 * PEAK_RATE))
                peak = max(self.peaks[start:end], default=0)
                extent = max(1, min(1, peak ** 0.7) * (height / 2 - 30))
                cr.rectangle(left + index * 3, center - extent, 1.8, extent * 2)
            cr.fill()
        else:
            color(c["muted_text"])
            cr.set_font_size(11)
            text_width = cr.text_extents(self.placeholder).width
            cr.move_to(max(left, (width - text_width) / 2), center - 10)
            cr.show_text(self.placeholder)
        position = self.window.position
        if 0 <= position <= total:
            x = left + position / total * span
            color(c["error_color"] if self.window.engine.mode == "record" else c["foreground"], 0.9)
            cr.set_line_width(1)
            cr.move_to(x + 0.5, 23)
            cr.line_to(x + 0.5, height - 12)
            cr.stroke()
            cr.move_to(x - 4, 23)
            cr.line_to(x + 4, 23)
            cr.line_to(x, 28)
            cr.close_path()
            cr.fill()


class BeatGrid(Waveform):
    def __init__(self, window):
        super().__init__(window, "metronome_color", "")

    def draw(self, _area, cr, width, height):
        def color(token, alpha=1):
            value = self.window.colors[token]
            cr.set_source_rgba(*(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)), alpha)

        settings = self.window.metronome_settings
        total, position = self.window.duration, self.window.position
        left, span, period = 20, max(1, width - 40), 60 / settings.bpm
        offset = settings.alignment_ms / 1000
        spacing = span * period / total
        stride = max(1, math.ceil(10 / spacing))
        if stride > 1:
            stride = math.ceil(stride / 4) * 4
        active = settings.enabled and self.window.engine.mode in ("record", "play")
        current = math.floor((position - offset) / period)
        cr.select_font_face("monospace", 0, 0)
        cr.set_font_size(9)
        first = max(0, math.ceil(-offset / period))
        first = math.ceil(first / stride) * stride
        for beat in range(first, math.floor((total - offset) / period) + 1, stride):
            x = left + (offset + beat * period) / total * span
            downbeat = beat % 4 == 0
            color("metronome_color", (0.9 if downbeat else 0.45) if settings.enabled else 0.2)
            cr.set_line_width(2 if downbeat else 1)
            cr.move_to(x + 0.5, 36 if downbeat else 48)
            cr.line_to(x + 0.5, height - 36)
            cr.stroke()
            if active and beat == current and (position - offset) % period < 0.12:
                color("metronome_color", 0.18)
                cr.rectangle(x, 30, min(spacing, 22), height - 60)
                cr.fill()
            if spacing >= 18 or downbeat:
                color("muted_text")
                cr.move_to(x + 3, 22)
                cr.show_text(str(beat % 4 + 1))
        color("muted_text")
        cr.move_to(left, height - 13)
        cr.show_text("4/4  ·  Not in exports")
        if 0 <= position <= total:
            color("error_color" if self.window.engine.mode == "record" else "foreground", 0.9)
            x = left + position / total * span
            cr.set_line_width(1)
            cr.move_to(x + 0.5, 28)
            cr.line_to(x + 0.5, height - 28)
            cr.stroke()


class BackingDialog(Gtk.Window):
    def __init__(self, parent):
        super().__init__(title="Add backing track", transient_for=parent, modal=True,
                         destroy_with_parent=True, default_width=520, resizable=False)
        self.add_css_class("riff")
        self.owner, self.job = parent, None
        self._pulse = None
        self._fraction = None
        body = margins(box(vertical=True, spacing=16), 24)
        body.append(label("Add a backing track", ["track-name"]))
        body.append(label("Paste a YouTube link or choose an audio file.", ["small", "muted"]))
        self.url = Gtk.Entry(placeholder_text="https://www.youtube.com/watch?v=…", hexpand=True)
        self.url.set_input_purpose(Gtk.InputPurpose.URL)
        self.url.connect("activate", lambda *_: self.begin())
        body.append(self.url)
        self.status = label("Audio will be saved with this session.", ["small", "muted"])
        self.status.set_wrap(True)
        self.status.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.status.set_lines(4)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(55)
        body.append(self.status)
        self.progress_bar = Gtk.ProgressBar(show_text=True, visible=False)
        body.append(self.progress_bar)
        actions = box(spacing=8)
        self.file_button = button("Choose file…", callback=self.choose_file, classes=["outlined"])
        actions.append(self.file_button)
        actions.append(Gtk.Box(hexpand=True))
        self.cancel_button = button("Cancel", callback=self.close, classes=["flat"])
        actions.append(self.cancel_button)
        self.download_button = button("Download & add", callback=self.begin, classes=["export"])
        actions.append(self.download_button)
        body.append(actions)
        self.set_child(body)
        self.set_default_widget(self.download_button)
        self.connect("close-request", self.close_requested)

    def choose_file(self):
        self.close()
        self.owner.import_dialog()

    def begin(self):
        if self.job or self.owner.busy or self.owner.engine.mode == "record":
            return
        try:
            job = YouTubeDownload(self.url.get_text(), self.owner.session)
        except ValueError as error:
            self.show_status(str(error), True)
            self.url.grab_focus()
            return
        self.job = job
        job.progress = lambda text, fraction: GLib.idle_add(self.update_progress, job, text, fraction)
        self.owner.stop(meter=False)
        self.owner.busy = True
        for widget in (self.url, self.file_button, self.download_button):
            widget.set_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self._fraction = None
        self.progress_bar.set_visible(True)
        self.progress_bar.set_text("Connecting…")
        self.show_status("Connecting to YouTube…")
        self._pulse = GLib.timeout_add(100, self.pulse)

        def worker():
            try:
                item = job.run()
                GLib.idle_add(self.finished, job, item, None, False)
            except DownloadCancelled:
                GLib.idle_add(self.finished, job, None, None, True)
            except Exception as error:
                GLib.idle_add(self.finished, job, None, str(error), False)

        threading.Thread(target=worker, daemon=True).start()

    def pulse(self):
        if self._fraction is None:
            self.progress_bar.pulse()
        return True

    def show_status(self, message, error=False):
        self.status.set_text(message)
        self.status.set_tooltip_text(message)
        self.status.remove_css_class("muted")
        self.status.remove_css_class("error")
        self.status.add_css_class("error" if error else "muted")

    def update_progress(self, job, message, fraction):
        if job != self.job or job.cancelled.is_set():
            return
        self._fraction = fraction
        self.show_status(message)
        self.progress_bar.set_text(f"{fraction:.0%}" if fraction is not None else message)
        if fraction is not None:
            self.progress_bar.set_fraction(fraction)
        self.owner.notice(message + (f" {fraction:.0%}" if fraction is not None else ""))

    def finished(self, job, item, error, cancelled):
        if job != self.job:
            return
        if job.cancelled.is_set():
            cancelled, error = True, None
            if item:
                try:
                    self.owner.session.path(item).unlink(missing_ok=True)
                except OSError as cleanup_error:
                    error = f"Could not remove the cancelled download: {cleanup_error}"
        self.job = None
        self.owner.busy = False
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = None
        if error:
            self.show_status(error, True)
            self.owner.notice("YouTube import failed: " + error, True)
            self.progress_bar.set_visible(False)
            for widget in (self.url, self.file_button, self.download_button, self.cancel_button):
                widget.set_sensitive(True)
        else:
            if cancelled:
                self.owner.notice("Download cancelled")
            else:
                self.owner.use_backing(item)
            self.close()
        self.owner.start_meter()

    def close_requested(self, *_):
        if self.job:
            self.job.cancel()
            self.show_status("Cancelling download…")
            self.cancel_button.set_sensitive(False)
            return True
        self.owner._backing_dialog = None
        return False


class Window(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title="Riff", default_width=1060, default_height=760)
        self.add_css_class("riff")
        self.set_size_request(540, 320)
        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(self.get_display(), self._css_provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme(theme.colors())
        self.settings = read_json(STATE_DIR / "settings.json")
        self.engine = Engine(self.audio_error, self.audio_end)
        self.inputs, self.outputs = devices()
        self.input = next((d for d in self.inputs if d.name == self.settings.get("input")),
                          self.inputs[0] if self.inputs and not self.settings.get("input") else None)
        self.output = next((d for d in self.outputs if d.name == self.settings.get("output")),
                           self.outputs[0] if self.outputs and not self.settings.get("output") else None)
        if self.input:
            self.settings["input"] = self.input.name
        if self.output:
            self.settings["output"] = self.output.name
        self.pair = tuple(self.settings.get("pair", [2, 3] if self.input and self.input.channels >= 4 else [0, 1]))
        if self.input and max(self.pair) >= self.input.channels:
            self.pair = (0, min(1, self.input.channels - 1))
        self.position, self.busy, self.closed = 0.0, False, False
        self.pending_take = None
        self._backing_dialog = None
        self.loop = False
        self._loading = True
        self._peaks_version = 0
        self._meter_error = False
        self.tap_tempo = TapTempo()
        self._applying_tap = False
        try:
            self.session = Session.open(Path(self.settings["last_session"]))
        except (KeyError, ValueError, OSError):
            self.session = Session.new()
        self.build()
        self.load_session()
        self._loading = False
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.key_pressed)
        self.add_controller(keys)
        self.connect("close-request", self.close_request)
        self._timer = GLib.timeout_add(50, self.tick)
        # Polling survives Omarchy replacing the whole theme directory on a switch.
        self._theme_timer = GLib.timeout_add_seconds(1, self.refresh_theme)
        GLib.idle_add(self.start_meter)

    def apply_theme(self, palette):
        self.colors = palette
        self._css_provider.load_from_string(theme.css(palette))
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", palette["mode"] == "dark")
        for name in ("backing_wave", "guitar_wave", "metronome_wave"):
            if area := getattr(self, name, None):
                area.queue_draw()

    def refresh_theme(self):
        if self.closed:
            return False
        palette = theme.colors(fallback=self.colors)
        if palette != self.colors:
            self.apply_theme(palette)
        return True

    @property
    def duration(self):
        backing, take = self.session.data.get("backing"), self.session.selected
        return max(10, backing["duration"] if backing else 0, take["duration"] if take else 0,
                   math.ceil(self.position / 10) * 10)

    @property
    def metronome_settings(self):
        return MetronomeSettings.from_session(self.session.data)

    def build(self):
        header = Gtk.HeaderBar()
        brand = box(spacing=10, children=[label("≋", ["brand", "backing-accent"]), label("riff", ["brand"])])
        header.pack_start(brand)
        header.set_title_widget(label("GUITAR RECORDER", ["eyebrow"]))
        self.folder_button = button(icon="folder-open-symbolic", callback=self.open_folder,
                                    classes=["flat"], tooltip="Show session files")
        header.pack_end(button(icon="open-menu-symbolic", callback=self.show_menu, classes=["flat"], tooltip="Session & shortcuts"))
        header.pack_end(button(icon="emblem-system-symbolic", callback=self.audio_settings, classes=["flat"], tooltip="Audio settings"))
        header.pack_end(self.folder_button)
        self.set_titlebar(header)
        root = box(vertical=True)
        content = margins(box(vertical=True, spacing=12), 20)
        content.set_vexpand(True)
        title_row = box(spacing=12)
        title_info = box(vertical=True, spacing=3)
        self.name_entry = Gtk.Entry(hexpand=True, placeholder_text="Session name")
        self.name_entry.add_css_class("session-name")
        self.name_entry.connect("changed", self.name_changed)
        self.save_label = label("Saved locally · 48 kHz / 24-bit WAV", ["muted", "small"])
        title_info.append(self.name_entry)
        title_info.append(self.save_label)
        title_info.set_hexpand(True)
        title_row.append(title_info)
        self.new_button = button("New session", callback=self.new_session, classes=["outlined"])
        self.new_button.set_valign(Gtk.Align.CENTER)
        title_row.append(self.new_button)
        content.append(title_row)

        transport = box(spacing=8)
        transport.add_css_class("transport")
        self.record_button = button("●  Record", callback=self.record, classes=["record"], tooltip="Record a new take from the beginning · R")
        self.play_button = button(icon="media-playback-start-symbolic", callback=self.play, classes=["flat"], tooltip="Play / pause · Space")
        self.stop_button = button(icon="media-playback-stop-symbolic", callback=self.stop, classes=["flat"], tooltip="Stop · Esc")
        self.rewind_button = button(icon="media-skip-backward-symbolic", callback=lambda: self.seek(0), classes=["flat"], tooltip="Back to start · Home")
        for child in (self.record_button, self.play_button, self.stop_button, self.rewind_button):
            transport.append(child)
        self.loop_button = Gtk.ToggleButton(icon_name="media-playlist-repeat-symbolic", tooltip_text="Loop playback")
        self.loop_button.add_css_class("flat")
        self.loop_button.connect("toggled", lambda b: setattr(self, "loop", b.get_active()))
        transport.append(self.loop_button)
        spacer = Gtk.Box(hexpand=True)
        transport.append(spacer)
        self.transport_status = label("Ready", ["muted", "small"])
        transport.append(self.transport_status)
        self.clock = label("00:00.0", ["clock"], 1)
        self.clock.set_margin_start(12)
        transport.append(self.clock)
        content.append(transport)

        self.backing_wave = Waveform(self, "backing_color", "Add a file or YouTube link")
        self.guitar_wave = Waveform(self, "guitar_color", "Ready for your next take")
        self.track_controls = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.backing_name = label("No backing track", ["muted", "small"])
        self.guitar_name = label("No takes yet", ["muted", "small"])
        self.backing_slider = self.gain_slider("backing")
        self.guitar_slider = self.gain_slider("guitar")
        self.import_button = button("+ Add track", callback=self.add_backing_dialog, classes=["outlined", "small"], tooltip="Add a YouTube link or an audio file · Ctrl+O")
        content.append(self.lane("01", "Backing track", self.backing_name, self.backing_slider,
                                 self.backing_wave, "backing-accent", self.import_button))
        content.append(self.lane("02", "Guitar", self.guitar_name, self.guitar_slider,
                                 self.guitar_wave, "guitar-accent"))
        content.append(self.metronome_lane())
        take_heading = box(spacing=12, children=[label("TAKES", ["eyebrow"])])
        self.take_count = label("0", ["muted", "small", "mono"])
        take_heading.append(self.take_count)
        stretch = Gtk.Box(hexpand=True)
        take_heading.append(stretch)
        take_hint = label("Every take is saved. Keep playing.", ["muted", "small"])
        take_hint.set_ellipsize(Pango.EllipsizeMode.END)
        take_heading.append(take_hint)
        content.append(take_heading)
        self.takes_box = box(spacing=8)
        takes_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                         vscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=40)
        takes_scroll.set_child(self.takes_box)
        content.append(takes_scroll)
        bottom = box(spacing=8)
        self.message = label("Space  play / pause     R  record     Esc  stop", ["muted", "small", "mono"])
        self.message.set_ellipsize(Pango.EllipsizeMode.END)
        self.message.set_hexpand(True)
        bottom.append(self.message)
        self.export_guitar_button = button("Guitar only", callback=lambda: self.export_dialog(True), classes=["outlined"], tooltip="Export the selected guitar take")
        self.export_button = button("Export mix  ↗", callback=self.export_dialog, classes=["export"], tooltip="Export backing + selected take · Ctrl+E")
        bottom.append(self.export_guitar_button)
        bottom.append(self.export_button)
        content.append(bottom)
        content_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                                            vexpand=True, min_content_height=150)
        content_scroll.set_child(content)
        root.append(content_scroll)

        footer = box(spacing=16)
        footer.add_css_class("footer")
        self.input_label = label("", ["small", "muted"])
        self.input_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        footer.append(label("●", ["status-dot"]))
        footer.append(self.input_label)
        self.meter = Gtk.LevelBar(min_value=0, max_value=1, value=0)
        self.meter.set_size_request(100, -1)
        self.meter.set_valign(Gtk.Align.CENTER)
        footer.append(self.meter)
        self.db_label = label("−∞ dB", ["mono", "small", "muted"])
        self.db_label.set_width_chars(9)
        footer.append(self.db_label)
        end = label("Direct monitoring on Quad Cortex", ["small", "muted"], 1)
        end.set_hexpand(True)
        end.set_ellipsize(Pango.EllipsizeMode.END)
        footer.append(end)
        root.append(footer)
        self.set_child(root)
        target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        target.connect("drop", self.drop_file)
        self.backing_wave.add_controller(target)

    def lane(self, number, title, subtitle, slider, waveform, accent_class, action=None):
        row = box()
        row.add_css_class("lane")
        row.add_css_class("backing-lane" if accent_class == "backing-accent" else "guitar-lane")
        controls = box(vertical=True, spacing=6)
        controls.add_css_class("lane-controls")
        controls.set_size_request(188, -1)
        controls.set_hexpand(False)
        self.track_controls.add_widget(controls)
        controls.append(box(spacing=10, children=[label(number, ["mono", "small", accent_class]),
                                               label(title, ["track-name"])]))
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_max_width_chars(24)
        controls.append(subtitle)
        if action:
            action.set_halign(Gtk.Align.START)
            controls.append(action)
        else:
            controls.append(label("USB input · stereo", ["small", "muted"]))
        controls.append(slider)
        row.append(controls)
        row.append(waveform)
        return row

    def gain_slider(self, name):
        slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1.5, 0.01)
        slider.set_draw_value(False)
        slider.set_tooltip_text("Playback / export volume · recording stays unaltered")
        slider.connect("value-changed", self.gain_changed, name)
        return slider

    def metronome_lane(self):
        row = box()
        row.add_css_class("lane")
        row.add_css_class("metronome-lane")
        controls = box(vertical=True, spacing=6)
        controls.add_css_class("lane-controls")
        controls.set_size_request(188, -1)
        controls.set_hexpand(False)
        self.track_controls.add_widget(controls)
        controls.append(box(spacing=10, children=[label("03", ["mono", "small", "metronome-accent"]),
                                               label("Metronome", ["track-name"])]))
        controls.append(label("Monitor only", ["muted", "small"]))
        tempo = box(spacing=6)
        self.metronome_bpm = Gtk.SpinButton.new_with_range(40, 300, 1)
        self.metronome_bpm.set_width_chars(3)
        self.metronome_bpm.set_numeric(True)
        self.metronome_bpm.set_tooltip_text("Tempo in beats per minute · 4/4")
        self.metronome_bpm.connect("value-changed", self.metronome_bpm_changed)
        tempo.append(self.metronome_bpm)
        tempo.append(label("BPM", ["small", "muted"]))
        tempo.append(Gtk.Box(hexpand=True))
        self.metronome_tap = button("Tap", callback=self.tap_metronome,
                                    tooltip="Tap a steady beat to set BPM · T · pause 3 seconds to start over")
        tempo.append(self.metronome_tap)
        self.metronome_toggle = Gtk.ToggleButton(label="Off", tooltip_text="Toggle click during playback / recording · M")
        self.metronome_toggle.connect("toggled", self.metronome_changed)
        tempo.append(self.metronome_toggle)
        controls.append(tempo)
        alignment = box(spacing=6, children=[label("Align", ["small", "muted"])])
        self.metronome_alignment = Gtk.SpinButton.new_with_range(-ALIGNMENT_LIMIT_MS, ALIGNMENT_LIMIT_MS, 1)
        self.metronome_alignment.set_width_chars(6)
        self.metronome_alignment.set_numeric(True)
        self.metronome_alignment.get_adjustment().set_page_increment(100)
        self.metronome_alignment.set_tooltip_text("Click alignment in milliseconds · − earlier / + later")
        self.metronome_alignment.connect("value-changed", self.metronome_changed)
        alignment.append(self.metronome_alignment)
        alignment.append(label("ms", ["small", "muted"]))
        self.metronome_reset = button(icon="edit-undo-symbolic",
                                     callback=lambda: self.metronome_alignment.set_value(0),
                                     classes=["flat"], tooltip="Reset click alignment to 0 ms")
        alignment.append(self.metronome_reset)
        controls.append(alignment)
        self.metronome_slider = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.metronome_slider.set_draw_value(False)
        self.metronome_slider.set_tooltip_text("Click volume · never recorded or exported")
        self.metronome_slider.connect("value-changed", self.metronome_changed)
        controls.append(self.metronome_slider)
        row.append(controls)
        self.metronome_wave = BeatGrid(self)
        row.append(self.metronome_wave)
        return row

    def metronome_bpm_changed(self, *_):
        if not self._applying_tap:
            self.tap_tempo.reset()
        self.metronome_changed()

    def tap_metronome(self):
        if self.busy:
            return
        bpm = self.tap_tempo.tap()
        if bpm is None:
            self.notice("Tap again to set the tempo")
            return
        self._applying_tap = True
        try:
            self.metronome_bpm.set_value(bpm)
        finally:
            self._applying_tap = False
        self.notice(f"Tap tempo · {bpm} BPM")

    def metronome_changed(self, *_):
        if self._loading:
            return
        enabled = self.metronome_toggle.get_active()
        if enabled and not self.output:
            self.metronome_toggle.set_active(False)
            self.notice("Choose a playback output in Audio settings first.", True)
            return
        self.metronome_toggle.set_label("On" if enabled else "Off")
        self.session.data.update(metronome_enabled=enabled,
                                 metronome_bpm=self.metronome_bpm.get_value_as_int(),
                                 metronome_gain=self.metronome_slider.get_value(),
                                 metronome_alignment_ms=self.metronome_alignment.get_value_as_int())
        self.engine.set_metronome(self.metronome_settings)
        if not enabled and self.engine.mode in ("play", "pause") and not (
                self.session.selected or self.session.data.get("backing")):
            self.stop()
        self.save()
        self.metronome_wave.queue_draw()

    def gain_changed(self, slider, name):
        if self._loading:
            return
        self.session.data[name + "_gain"] = slider.get_value()
        self.engine.set_gain(name, slider.get_value())
        self.save()

    def name_changed(self, entry):
        if not self._loading:
            self.session.data["name"] = entry.get_text().strip() or "Untitled session"
            self.save()

    def save(self):
        try:
            self.session.save()
            self.settings["last_session"] = str(self.session.folder)
            atomic_json(STATE_DIR / "settings.json", self.settings)
        except OSError as error:
            self.notice(f"Could not save session: {error}", True)

    def notice(self, text, error=False):
        self.message.set_text(text)
        self.message.set_tooltip_text(text)
        self.message.remove_css_class("muted")
        self.message.remove_css_class("error")
        self.message.add_css_class("error" if error else "muted")

    def load_session(self):
        self._loading = True
        self.tap_tempo.reset()
        self.position = 0
        self.name_entry.set_text(self.session.data["name"])
        self.backing_slider.set_value(self.session.data.get("backing_gain", 0.65))
        self.guitar_slider.set_value(self.session.data.get("guitar_gain", 1.0))
        metronome = self.metronome_settings
        self.metronome_bpm.set_value(metronome.bpm)
        self.metronome_slider.set_value(metronome.gain)
        self.metronome_alignment.set_value(metronome.alignment_ms)
        self.metronome_toggle.set_active(metronome.enabled)
        self.metronome_toggle.set_label("On" if metronome.enabled else "Off")
        self._loading = False
        self.refresh_tracks()
        self.refresh_takes()
        self.save()

    def refresh_tracks(self):
        self._peaks_version += 1
        version = self._peaks_version
        backing, take = self.session.data.get("backing"), self.session.selected
        self.backing_name.set_text(backing["name"] if backing else "No backing track")
        self.backing_name.set_tooltip_text(backing.get("source_url", backing["name"]) if backing else None)
        self.guitar_name.set_text(take["name"] if take else "No takes yet")
        self.import_button.set_label("Replace track" if backing else "+ Add track")
        for area, item in ((self.backing_wave, backing), (self.guitar_wave, take)):
            area.peaks, area.duration = [], item["duration"] if item else 0
            if item:
                path = self.session.path(item)

                def worker(area=area, path=path):
                    try:
                        peaks = read_peaks(path)
                        GLib.idle_add(self.apply_peaks, area, peaks, version)
                    except (OSError, ValueError) as error:
                        GLib.idle_add(self.notice, f"Could not read waveform: {error}", True)

                threading.Thread(target=worker, daemon=True).start()

    def apply_peaks(self, area, peaks, version):
        if not self.closed and version == self._peaks_version:
            area.peaks = peaks
            area.queue_draw()

    def refresh_takes(self):
        while child := self.takes_box.get_first_child():
            self.takes_box.remove(child)
        self.take_count.set_text(str(len(self.session.data["takes"])))
        if not self.session.data["takes"]:
            self.takes_box.append(label("Hit record to make your first take.", ["muted", "small"]))
        for take in self.session.data["takes"]:
            selected = take["id"] == self.session.data.get("selected")
            child = button(f"{'●  ' if selected else ''}{take['name']}   {timecode(take['duration'])}",
                           callback=lambda ident=take["id"]: self.select_take(ident),
                           classes=["take", *(["selected"] if selected else [])])
            self.takes_box.append(child)

    def select_take(self, ident):
        if self.busy or self.engine.mode == "record":
            return
        self.stop()
        self.session.data["selected"] = ident
        self.save()
        self.refresh_tracks()
        self.refresh_takes()

    def start_meter(self):
        if self.closed or self.busy or self.engine.mode in ("record", "play", "pause"):
            return False
        if not self.input:
            self.input_label.set_text("No input · open Audio settings")
            return False
        channels = f"{self.pair[0] + 1}/{self.pair[1] + 1}" if self.pair[0] != self.pair[1] else str(self.pair[0] + 1)
        self.input_label.set_text(f"{self.input.label}  ·  {channels}")
        try:
            self.engine.capture(self.input, self.pair)
            self._meter_error = False
        except Exception as error:
            self._meter_error = True
            self.notice(f"Input unavailable: {error}", True)
        return False

    def record(self):
        if self.busy:
            return
        if self.engine.mode == "record":
            self.stop()
            return
        if not self.input:
            self.notice("Choose an input in Audio settings first.", True)
            return
        self.engine.stop()
        self.position = 0
        self.pending_take = self.session.next_take()
        self._peaks_version += 1
        self.guitar_wave.peaks = []
        backing = self.session.data.get("backing")
        if (backing or self.metronome_settings.enabled) and not self.output:
            self.pending_take = None
            self.notice("Choose a playback output in Audio settings first.", True)
            self.start_meter()
            return
        try:
            self.engine.capture(self.input, self.pair, self.session.path(self.pending_take),
                                self.session.path(backing) if backing else None,
                                self.backing_slider.get_value(), self.output,
                                metronome=self.metronome_settings)
            self.guitar_name.set_text(self.pending_take["name"] + " · recording")
            self.notice("Recording a new take · press R or Esc to stop")
        except Exception as error:
            self.finish_take()
            self.notice(str(error), True)

    def finish_take(self):
        self.engine.stop()
        if self.pending_take:
            take, self.pending_take = self.pending_take, None
            try:
                self.session.commit_take(take)
                self.save()
            except (OSError, ValueError) as error:
                self.notice(f"Could not finish the take: {error}", True)
            self.refresh_tracks()
            self.refresh_takes()

    def stop(self, meter=True):
        was_recording = self.pending_take is not None
        self.finish_take()
        self.position = 0
        if was_recording:
            self.notice("Take saved · ready for another")
        if meter:
            self.start_meter()

    def play(self):
        if self.busy or self.engine.mode == "record":
            return
        if self.engine.mode in ("play", "pause"):
            self.engine.pause()
            return
        backing, take = self.session.data.get("backing"), self.session.selected
        if not backing and not take and not self.metronome_settings.enabled:
            self.notice("Add a backing track, record a take, or enable the metronome.")
            return
        if not self.output:
            self.notice("Choose a playback output in Audio settings first.", True)
            return
        try:
            self.engine.play(self.session.path(backing) if backing else None,
                             self.session.path(take) if take else None, self.output,
                             self.backing_slider.get_value(), self.guitar_slider.get_value(),
                             self.position, self.session.data.get("alignment_ms", 0),
                             metronome=self.metronome_settings)
        except Exception as error:
            self.audio_error(str(error))

    def seek(self, seconds):
        if self.busy or self.engine.mode == "record":
            return
        self.position = max(0, min(self.duration - 0.05, seconds))
        self.engine.seek(self.position)

    def audio_error(self, message):
        # Preserve any audio already written, and avoid retrying a failed device in a loop.
        self.stop(meter=False)
        self._meter_error = True
        self.notice(f"Audio stopped: {message} · check Audio settings", True)

    def audio_end(self):
        if self.engine.mode == "play" and self.loop:
            self.engine.seek(0)
            return
        if self.engine.mode == "meter":
            self.engine.stop()
        else:
            self.stop()

    def tick(self):
        if self.closed:
            return False
        mode = self.engine.mode
        if mode in ("record", "play", "pause"):
            self.position = self.engine.position
        if mode == "record":
            self.guitar_wave.peaks = self.engine.peaks
            self.guitar_wave.duration = self.position
            if self.position >= self.engine.record_limit:
                self.stop()
                self.notice("One-hour take saved. Press Record to start another.")
        self.clock.set_text(timecode(self.position, True))
        self.transport_status.set_visible(self.get_width() >= 640)
        self.record_button.set_label("■  Finish take" if mode == "record" else "●  Record")
        self.play_button.set_icon_name("media-playback-pause-symbolic" if mode == "play" else "media-playback-start-symbolic")
        self.transport_status.set_text("Recording" if mode == "record" else "Playing" if mode == "play" else
                                       "Paused" if mode == "pause" else "Working…" if self.busy else "Ready")
        self.meter.set_value(amplitude(self.engine.peak_db))
        self.db_label.set_text(f"{self.engine.peak_db:.1f} dB" if self.engine.peak_db > -80 else "−∞ dB")
        if self.engine.peak_db > -0.5:
            self.db_label.add_css_class("error")
        else:
            self.db_label.remove_css_class("error")
        recording = mode == "record"
        for child in (self.new_button, self.import_button, self.rewind_button, self.loop_button):
            child.set_sensitive(not self.busy and not recording)
        self.record_button.set_sensitive(not self.busy and self.input is not None)
        self.play_button.set_sensitive(not self.busy and not recording and
                                       bool(self.session.selected or self.session.data.get("backing") or
                                            self.metronome_settings.enabled))
        self.export_button.set_sensitive(not self.busy and not recording and
                                         bool(self.session.selected or self.session.data.get("backing")))
        self.export_guitar_button.set_sensitive(not self.busy and not recording and bool(self.session.selected))
        self.takes_box.set_sensitive(not self.busy and not recording)
        for child in (self.metronome_toggle, self.metronome_bpm, self.metronome_tap, self.metronome_slider,
                      self.metronome_alignment, self.metronome_reset):
            child.set_sensitive(not self.busy)
        for area in (self.backing_wave, self.guitar_wave, self.metronome_wave):
            area.queue_draw()
        return True

    def add_backing_dialog(self):
        if self.busy or self.engine.mode == "record":
            return
        if not self._backing_dialog:
            self._backing_dialog = BackingDialog(self)
        self._backing_dialog.present()
        self._backing_dialog.url.grab_focus()

    def import_dialog(self):
        if self.busy or self.engine.mode == "record":
            return
        dialog = Gtk.FileDialog(title="Add a backing track")
        audio_filter = Gtk.FileFilter(name="Audio files")
        audio_filter.add_mime_type("audio/*")
        for extension in ("wav", "mp3", "flac", "ogg", "m4a", "aif", "aiff", "opus"):
            audio_filter.add_pattern("*." + extension)
        dialog.set_default_filter(audio_filter)

        def chosen(dialog, result):
            try:
                file = dialog.open_finish(result)
                if file and file.get_path():
                    self.import_file(Path(file.get_path()))
            except GLib.Error:
                pass

        dialog.open(self, None, chosen)

    def drop_file(self, _target, file, _x, _y):
        if not self.busy and self.engine.mode != "record" and file.get_path():
            self.import_file(Path(file.get_path()))
            return True
        return False

    def work(self, function, done, text):
        self.busy = True
        self.notice(text)

        def worker():
            try:
                value = function()
                GLib.idle_add(finished, value, None)
            except Exception as error:
                GLib.idle_add(finished, None, str(error))

        def finished(value, error):
            self.busy = False
            if self.closed:
                return
            if error:
                self.notice(error, True)
            else:
                done(value)
            self.start_meter()

        threading.Thread(target=worker, daemon=True).start()

    def import_file(self, path):
        self.stop(meter=False)
        self.work(lambda: import_backing(path, self.session), self.use_backing, "Importing backing track…")

    def use_backing(self, item):
        self.session.data["backing"] = item
        self.save()
        self.refresh_tracks()
        self.notice("Backing track ready · Record starts a fresh take from the beginning")

    def export_dialog(self, guitar_only=False):
        if self.busy or self.engine.mode == "record":
            return
        dialog = Gtk.FileDialog(title="Export guitar" if guitar_only else "Export mix")
        stem = self.session.data["name"].replace("/", "-")
        dialog.set_initial_name(stem + ("-guitar.wav" if guitar_only else "-mix.wav"))

        def chosen(dialog, result):
            try:
                file = dialog.save_finish(result)
                if not file or not file.get_path():
                    return
                path = Path(file.get_path())
                if path.suffix.lower() != ".wav":
                    path = path.with_name(path.name + ".wav")
                protected = [self.session.folder / "session.json", *[self.session.path(t) for t in self.session.data["takes"]]]
                if self.session.data.get("backing"):
                    protected.append(self.session.path(self.session.data["backing"]))
                if path.resolve() in [p.resolve() for p in protected]:
                    self.notice("Choose a new export filename to preserve your session audio.", True)
                    return
                self.stop(meter=False)
                self.work(lambda: export_audio(self.session, path, guitar_only),
                          lambda _: self.notice(f"Exported {path.name} · 48 kHz / 24-bit WAV"), "Exporting WAV…")
            except GLib.Error:
                pass

        dialog.save(self, None, chosen)

    def new_session(self):
        if self.busy or self.engine.mode == "record":
            return
        self.stop(meter=False)
        self.session = Session.new()
        self.load_session()
        self.notice("New session · add a backing track, or just hit record")
        self.start_meter()

    def open_session(self):
        if self.busy or self.engine.mode == "record":
            return
        dialog = Gtk.FileDialog(title="Open a Riff session folder")

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
                if folder and folder.get_path():
                    session = Session.open(Path(folder.get_path()))
                    self.stop(meter=False)
                    self.session = session
                    self.load_session()
                    self.start_meter()
                    self.notice("Session opened")
            except GLib.Error:
                pass
            except (ValueError, OSError) as error:
                self.notice(str(error), True)

        dialog.select_folder(self, None, chosen)

    def open_folder(self):
        try:
            Gio.AppInfo.launch_default_for_uri(self.session.folder.as_uri(), None)
        except GLib.Error as error:
            self.notice(str(error), True)

    def show_menu(self):
        dialog = Gtk.Window(title="Riff", transient_for=self, modal=True, default_width=380)
        dialog.add_css_class("riff")
        body = margins(box(vertical=True, spacing=16), 24)
        body.append(label("A little room to play.", ["track-name"]))
        body.append(button("Open saved session…", callback=lambda: (dialog.close(), self.open_session()), classes=["outlined"]))
        for text in ("Space         Play / pause", "R                 Record / finish take", "M                Toggle metronome", "T                 Tap tempo", "Esc              Stop", "Home          Back to start", "Ctrl+O         Add backing track", "Ctrl+N         New session", "Ctrl+E          Export mix"):
            body.append(label(text, ["mono", "small", "muted"]))
        hint = label("Sessions & takes live in ~/Music/Riff.\nNo account. No cloud. Just audio.", ["small", "muted"])
        body.append(hint)
        body.append(button("Close", callback=dialog.close, classes=["outlined"]))
        dialog.set_child(body)
        dialog.present()

    def audio_settings(self):
        if self.busy or self.engine.mode == "record":
            self.notice("Finish the take before changing audio settings.")
            return
        dialog = Gtk.Window(title="Audio settings", transient_for=self, modal=True, default_width=500)
        dialog.add_css_class("riff")
        body = margins(box(vertical=True, spacing=12), 24)
        body.append(label("Audio settings", ["track-name"]))
        body.append(label("Input device", ["small", "muted"]))
        source_drop = Gtk.DropDown()
        source_drop.set_hexpand(True)
        body.append(source_drop)
        body.append(label("Record channels", ["small", "muted"]))
        pair_drop = Gtk.DropDown()
        body.append(pair_drop)
        body.append(label("Playback output · channels 1/2", ["small", "muted"]))
        output_drop = Gtk.DropDown()
        body.append(output_drop)
        pairs = []
        available_inputs, available_outputs = [], []

        def update_pairs(*_):
            index = source_drop.get_selected()
            source = available_inputs[index] if index < len(available_inputs) else None
            pairs.clear()
            names = []
            if source:
                for channel in range(0, source.channels - 1, 2):
                    pairs.append((channel, channel + 1))
                    suffix = " · processed guitar*" if channel == 2 and "cortex" in source.label.lower() else ""
                    names.append(f"USB {channel + 1} + {channel + 2}{suffix}")
                for channel in range(source.channels):
                    pairs.append((channel, channel))
                    names.append(f"USB {channel + 1} · mono")
            pair_drop.set_model(Gtk.StringList.new(names or ["No channels available"]))
            preferred = self.pair if source and self.input and source.name == self.input.name else ((2, 3) if source and source.channels >= 4 and "cortex" in source.label.lower() else (0, min(1, source.channels - 1)) if source else (0, 0))
            pair_drop.set_selected(pairs.index(preferred) if preferred in pairs else 0)

        source_drop.connect("notify::selected", update_pairs)

        def refresh():
            nonlocal available_inputs, available_outputs
            available_inputs, available_outputs = devices()
            source_drop.set_model(Gtk.StringList.new([d.label for d in available_inputs] or ["No input devices found"]))
            output_drop.set_model(Gtk.StringList.new([d.label for d in available_outputs] or ["No output devices found"]))
            source_drop.set_selected(next((i for i, d in enumerate(available_inputs) if self.input and d.name == self.input.name), 0))
            output_drop.set_selected(next((i for i, d in enumerate(available_outputs) if self.output and d.name == self.output.name), 0))
            update_pairs()

        refresh()
        body.append(label("Guitar alignment · milliseconds", ["small", "muted"]))
        alignment = Gtk.SpinButton.new_with_range(-500, 500, 1)
        alignment.set_value(self.session.data.get("alignment_ms", 0))
        body.append(alignment)
        hint = label("* Quad Cortex usually sends processed audio on USB 3/4.\nIf USB Dry/Wet Swap is enabled, choose 1/2 instead.\nMonitor through the Quad Cortex headphones or outputs.\n\nAlignment shifts the take for playback and export.\nUse a negative value if your recording sounds late.", ["small", "muted"])
        hint.set_wrap(True)
        body.append(hint)
        actions = box(spacing=8)
        actions.append(button("Refresh devices", callback=refresh, classes=["outlined"]))
        spacer = Gtk.Box(hexpand=True)
        actions.append(spacer)
        actions.append(button("Cancel", callback=dialog.close, classes=["flat"]))

        def apply():
            if source_drop.get_selected() >= len(available_inputs) or not pairs:
                return
            self.stop(meter=False)
            self.input = available_inputs[source_drop.get_selected()]
            self.pair = pairs[pair_drop.get_selected()]
            self.output = available_outputs[output_drop.get_selected()] if output_drop.get_selected() < len(available_outputs) else None
            self.settings.update({"input": self.input.name, "pair": list(self.pair),
                                  "output": self.output.name if self.output else None})
            self.session.data["alignment_ms"] = alignment.get_value_as_int()
            self.save()
            dialog.close()
            self.notice("Audio settings saved")
            self.start_meter()

        actions.append(button("Apply", callback=apply, classes=["export"]))
        body.append(actions)
        dialog.set_child(body)
        dialog.present()

    def key_pressed(self, _controller, key, _code, modifiers):
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.Text)):
            return False
        ctrl = bool(modifiers & Gdk.ModifierType.CONTROL_MASK)
        if ctrl:
            actions = {Gdk.KEY_o: self.add_backing_dialog, Gdk.KEY_n: self.new_session, Gdk.KEY_e: self.export_dialog}
        else:
            actions = {Gdk.KEY_space: self.play, Gdk.KEY_r: self.record, Gdk.KEY_R: self.record,
                       Gdk.KEY_m: self.toggle_metronome, Gdk.KEY_M: self.toggle_metronome,
                       Gdk.KEY_t: self.tap_metronome, Gdk.KEY_T: self.tap_metronome,
                       Gdk.KEY_Escape: self.stop, Gdk.KEY_Home: lambda: self.seek(0)}
        action = actions.get(key)
        if action:
            action()
            return True
        return False

    def toggle_metronome(self):
        if not self.busy:
            self.metronome_toggle.set_active(not self.metronome_toggle.get_active())

    def close_request(self, *_):
        if self.busy:
            self.notice("Finish or cancel the current operation before closing.")
            return True
        self.closed = True
        GLib.source_remove(self._theme_timer)
        self.finish_take()
        self.save()
        return False


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.riff.Recorder", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None

    def do_activate(self):
        if not self.window:
            self.window = Window(self)
        self.window.present()
        self.window.set_focus(None)


def main():
    application = Application()

    def quit_cleanly():
        if application.window:
            application.window.close()
        else:
            application.quit()
        return True

    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, quit_cleanly)
    return application.run(None)
