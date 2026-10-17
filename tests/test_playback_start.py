"""Exercise the UI transport actions with real audio and no display or devices."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import wave

from riff.audio import Device, Engine
from riff.storage import RATE, Session
from riff.timeline import Timeline
from riff.ui import Window
from test_audio import spin_until


class Transport:
    """Use the window's transport actions without constructing GTK widgets."""

    duration = Window.duration
    playback_start = Window.playback_start
    metronome_settings = Window.metronome_settings
    play = Window.play
    play_from = Window.play_from
    stop = Window.stop
    seek = Window.seek
    rewind = Window.rewind
    audio_end = Window.audio_end

    def __init__(self, session, errors):
        self.session, self.errors = session, errors
        self.busy, self.loop, self.ends = False, False, 0
        self.pending_take = None
        self.position = 0
        self.output = Device("test", "Fake output", 2)
        self.backing_slider = self.guitar_slider = SimpleNamespace(get_value=lambda: 0.5)
        self.engine = Engine(errors.append, self.ended)
        real_play = self.engine.play
        self.engine.play = lambda *args, **kwargs: real_play(*args, **kwargs, test_sink="fakesink sync=true")
        self.timeline = Timeline(self.duration)

    def update_timeline(self):
        self.timeline.set_duration(self.duration)

    def save(self):
        self.session.save()

    def notice(self, *args):
        pass

    def audio_error(self, error):
        self.errors.append(error)

    def finish_take(self):
        self.engine.stop()

    def start_meter(self):
        pass

    def ended(self):
        self.ends += 1
        self.audio_end()


class PlaybackStartIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = Session.new(self.root)
        path = self.session.folder / "backing.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 3, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0" * (2 * RATE * 6))
        self.session.data["backing"] = {"file": path.name, "name": "Test backing", "duration": 2}
        self.errors = []
        self.transport = Transport(self.session, self.errors)

    def tearDown(self):
        self.transport.engine.stop()
        self.temp.cleanup()

    def test_setting_start_plays_jumps_and_resumes_without_toggling_pause(self):
        app = self.transport
        app.play_from(0.5)
        spin_until(lambda: app.engine.position > 0.52 or self.errors)
        pipeline = app.engine.pipeline
        self.assertEqual(Session.open(self.session.folder).data["playback_start"], 0.5)
        app.play_from(1)
        spin_until(lambda: app.engine.position > 1.02 or self.errors)
        self.assertEqual(app.engine.mode, "play")
        self.assertIs(app.engine.pipeline, pipeline)
        app.engine.pause()
        app.play_from(0.75)
        spin_until(lambda: app.engine.position > 0.77 or self.errors)
        self.assertLess(app.engine.position, 1)
        self.assertEqual(app.engine.mode, "play")
        self.assertIs(app.engine.pipeline, pipeline)
        self.assertEqual(app.playback_start, 0.75)
        self.assertEqual(self.errors, [])

    def test_stop_replay_repeat_and_rewind_use_the_saved_start(self):
        app = self.transport
        app.play_from(1.7)
        app.stop(meter=False)
        self.assertEqual(app.position, 1.7)
        app.play()
        app.loop = True
        pipeline = app.engine.pipeline
        spin_until(lambda: app.ends > 0 or self.errors)
        spin_until(lambda: 1.72 < app.engine.position < 1.95 or self.errors)
        self.assertIs(app.engine.pipeline, pipeline)
        app.rewind()
        spin_until(lambda: 0.02 < app.engine.position < 0.3 or self.errors)
        self.assertEqual(app.playback_start, 0)
        self.assertEqual(Session.open(self.session.folder).data["playback_start"], 0)
        app.stop(meter=False)
        self.assertEqual(app.position, 0)
        self.assertEqual(self.errors, [])

    def test_start_is_bounded_and_cannot_interrupt_recording_or_busy_work(self):
        app = self.transport
        app.play_from(10)
        self.assertEqual(app.playback_start, 1.95)
        app.stop(meter=False)
        app.play_from(-10)
        self.assertEqual(app.playback_start, 0)
        app.stop(meter=False)
        app.busy = True
        app.play_from(1)
        self.assertEqual(app.playback_start, 0)
        self.assertEqual(app.engine.mode, "idle")
        app.busy = False
        app.session.data["playback_start"] = 0.5
        app.engine.capture(Device("test", "Silent input", 2), (0, 1), self.root / "take.wav",
                           source_override="audiotestsrc is-live=true wave=silence")
        spin_until(lambda: app.engine.position > 0.1 or self.errors)
        pipeline = app.engine.pipeline
        app.play_from(1)
        app.rewind()
        spin_until(lambda: app.engine.position > 0.2 or self.errors)
        self.assertEqual(app.engine.mode, "record")
        self.assertIs(app.engine.pipeline, pipeline)
        self.assertEqual(app.playback_start, 0.5)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
