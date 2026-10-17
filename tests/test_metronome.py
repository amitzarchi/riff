"""Render the monitoring mix and verify the click cannot reach saved audio."""

from array import array
from pathlib import Path
import tempfile
import time
import unittest
import wave

from riff.audio import Device, Engine, Gst
from riff.metronome import ClickSource, MetronomeSettings
from riff.storage import RATE, Session, export_audio, import_backing
from test_audio import pcm16, spin_until


class MetronomeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.errors, self.ended = [], False
        self.engine = Engine(self.errors.append, lambda: setattr(self, "ended", True))

    def tearDown(self):
        self.engine.stop()
        self.temp.cleanup()

    def silence(self, duration=1.1):
        path = self.root / "silence.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 3, RATE, 0, "NONE", "not compressed"))
            audio.writeframes(b"\0" * (round(duration * RATE) * 6))
        return path

    def render(self, backing=None, take=None, **kwargs):
        self.ended = False
        self.engine.play(backing, take, None,
                         test_sink="appsink name=render sync=false max-buffers=200", **kwargs)
        sink = self.engine.pipeline.get_by_name("render")
        rendered, timestamps = array("f"), []
        while sample := sink.emit("try-pull-sample", 2 * Gst.SECOND):
            buf = sample.get_buffer()
            timestamps.append(buf.pts / Gst.SECOND)
            rendered.frombytes(buf.extract_dup(0, buf.get_size()))
        spin_until(lambda: self.ended or self.errors)
        self.assertEqual(self.errors, [])
        self.engine.stop()
        return rendered, timestamps

    def click_times(self, samples):
        onsets, previous = [], -RATE
        for frame, value in enumerate(samples[::2]):
            if abs(value) > 0.01:
                if frame - previous > RATE * 0.03:
                    onsets.append(frame / RATE)
                previous = frame
        return onsets

    def test_click_timing_accent_and_no_cumulative_drift(self):
        click = ClickSource(137)
        period = RATE * 60 / 137
        for beat in (0, 1, 4, 10001):
            start = round(beat * period)
            samples = array("f", click.render(start - 10, 1000))[::2]
            self.assertTrue(all(value == 0 for value in samples[:11]))
            self.assertGreater(max(abs(value) for value in samples[11:]), 0.2)
            crossings = sum(a <= 0 < b for a, b in zip(samples[12:800], samples[13:801]))
            self.assertAlmostEqual(crossings / (788 / RATE), 1760 if beat % 4 == 0 else 1100, delta=80)
        start = round(10001 * period) - 100
        self.assertEqual(click.render(start, 2000), click.render(start, 700) + click.render(start + 700, 1300))

    def test_click_is_audible_only_in_monitor_mix_and_keeps_file_duration(self):
        backing = self.silence()
        settings = MetronomeSettings(True, 120, 0.4)
        rendered, _ = self.render(backing, metronome=settings)
        expected = array("f", ClickSource(120).render(0, round(1.1 * RATE)))
        self.assertEqual(len(rendered), len(expected))
        self.assertLess(max(abs(a - b * settings.gain) for a, b in zip(rendered, expected)), 0.00001)
        muted, _ = self.render(backing, metronome=MetronomeSettings(False))
        self.assertEqual(max(abs(value) for value in muted), 0)

    def test_alignment_moves_clicks_earlier_and_later_without_extending_backing(self):
        for offset, duration, expected in ((125, 1.1, [0.125, 0.625]),
                                           (-125, 1.1, [0.375, 0.875]),
                                           (3000, 3.6, [3, 3.5]),
                                           (-3000, 1.1, [0, 0.5, 1])):
            with self.subTest(offset=offset):
                backing = self.silence(duration)
                rendered, _ = self.render(backing, metronome=MetronomeSettings(True, 120, 0.5, offset))
                actual = self.click_times(rendered)
                self.assertEqual(len(actual), len(expected), actual)
                for onset, wanted in zip(actual, expected):
                    self.assertAlmostEqual(onset, wanted, delta=0.0005)
                self.assertAlmostEqual(len(rendered) / 2 / RATE, duration, delta=0.0001)
        # Seeking uses the same shifted timeline, rather than restarting the beat.
        rendered, timestamps = self.render(self.silence(), position=0.6,
                                          metronome=MetronomeSettings(True, 120, 0.5, 125))
        self.assertAlmostEqual(timestamps[0], 0.6, delta=0.001)
        self.assertAlmostEqual(self.click_times(rendered)[0], 0.025, delta=0.0005)
        # At a non-integer sample period, a long-running shifted beat stays aligned.
        click = ClickSource(137, alignment_ms=-125)
        position = round(10001 * RATE * 60 / 137) - 6000
        samples = array("f", click.render(position - 10, 1000))
        self.assertAlmostEqual(self.click_times(samples)[0], 10 / RATE, delta=0.0005)

    def test_live_alignment_changes_keep_the_existing_transport(self):
        buffers = []
        self.engine.play(None, None, None,
                         test_sink="fakesink name=monitor sync=true signal-handoffs=true",
                         metronome=MetronomeSettings(True, 120, 0.5))
        pipeline = self.engine.pipeline

        def receive(_sink, buffer, _pad):
            samples = array("f", buffer.extract_dup(0, buffer.get_size()))
            buffers.append((buffer.pts / Gst.SECOND, max(abs(v) for v in samples)))

        pipeline.get_by_name("monitor").connect("handoff", receive)
        spin_until(lambda: self.engine.position > 0.15 or self.errors)
        self.engine.set_metronome(MetronomeSettings(True, 120, 0.5, 125))
        spin_until(lambda: self.engine.position > 0.7 or self.errors)
        delayed = [pts for pts, peak in buffers if 0.2 < pts < 0.7 and peak > 0.01]
        self.assertTrue(delayed)
        self.assertAlmostEqual(min(delayed), 0.625, delta=0.012)
        self.engine.set_metronome(MetronomeSettings(True, 120, 0.5, -125))
        spin_until(lambda: self.engine.position > 1.05 or self.errors)
        earlier = [pts for pts, peak in buffers if 0.75 < pts < 1.05 and peak > 0.01]
        self.assertTrue(earlier)
        self.assertAlmostEqual(min(earlier), 0.875, delta=0.012)
        self.assertIs(self.engine.pipeline, pipeline)
        self.assertEqual(self.errors, [])

    def test_seek_loop_and_alignment_share_the_audio_timeline(self):
        backing = self.silence()
        rendered, timestamps = self.render(backing, position=0.6, metronome=MetronomeSettings(True))
        self.assertAlmostEqual(timestamps[0], 0.6, delta=0.001)
        self.assertLess(max(abs(v) for v in rendered[:round(0.39 * RATE) * 2]), 0.00001)
        self.assertGreater(max(abs(v) for v in rendered[round(0.4 * RATE) * 2:]), 0.05)
        for offset in (-100, 100):
            rendered, _ = self.render(take=backing, alignment_ms=offset, metronome=MetronomeSettings(True))
            self.assertAlmostEqual(len(rendered) / 2 / RATE, 1.1 + offset / 1000, delta=0.001)
        # A repeat is a flushing seek back to zero on the existing pipeline.
        self.ended = False
        self.engine.play(backing, None, None, test_sink="fakesink sync=true",
                         metronome=MetronomeSettings(True, alignment_ms=125))
        pipeline = self.engine.pipeline
        spin_until(lambda: self.ended or self.errors)
        self.ended = False
        self.assertTrue(self.engine.seek(0))
        spin_until(lambda: self.engine.position > 0.05 or self.errors)
        self.assertIs(self.engine.pipeline, pipeline)
        self.assertLess(self.engine.position, 0.4)
        self.assertEqual(self.errors, [])

    def test_standalone_pause_and_live_controls(self):
        buffers = []
        self.engine.play(None, None, None,
                         test_sink="fakesink name=monitor sync=true signal-handoffs=true",
                         metronome=MetronomeSettings(True, 240, 0))
        pipeline = self.engine.pipeline

        def receive(_sink, buffer, _pad):
            samples = array("f", buffer.extract_dup(0, buffer.get_size()))
            buffers.append((buffer.pts / Gst.SECOND, max(abs(v) for v in samples)))

        pipeline.get_by_name("monitor").connect("handoff", receive)
        spin_until(lambda: self.engine.position > 0.15 or self.errors)
        self.engine.pause()
        position = self.engine.position
        time.sleep(0.06)
        self.assertAlmostEqual(self.engine.position, position, delta=0.02)
        self.assertEqual(max(peak for _, peak in buffers), 0)
        self.engine.set_metronome(MetronomeSettings(True, 180, 0.5))
        self.assertTrue(self.engine.seek(0.3))
        buffers.clear()
        self.engine.pause()
        spin_until(lambda: any(peak > 0.05 for _, peak in buffers) or self.errors)
        self.assertIs(self.engine.pipeline, pipeline)
        self.assertEqual(self.engine.click_source.bpm, 180)
        self.engine.set_metronome(MetronomeSettings(False, 180, 0.5))
        spin_until(lambda: self.engine.position > 0.8 or self.errors)
        self.assertTrue(all(peak == 0 for pts, peak in buffers if pts > 0.6))
        self.assertFalse(self.ended)
        self.assertEqual(self.errors, [])

    def test_recording_and_both_exports_exclude_the_click(self):
        session = Session.new(self.root)
        session.data.update(metronome_enabled=True, metronome_bpm=240, metronome_gain=0.6,
                            metronome_alignment_ms=125)
        session.data["backing"] = import_backing(self.silence(0.25), session)
        take = session.next_take()
        peaks = []
        self.engine.capture(Device("test", "Silent input", 2), (0, 1), session.path(take),
                            backing=session.path(session.data["backing"]),
                            source_override="audiotestsrc is-live=true wave=silence",
                            test_sink="fakesink name=monitor sync=true signal-handoffs=true",
                            metronome=MetronomeSettings.from_session(session.data))

        def receive(_sink, buffer, _pad):
            samples = array("f", buffer.extract_dup(0, buffer.get_size()))
            peaks.append(max(abs(v) for v in samples))

        self.engine.pipeline.get_by_name("monitor").connect("handoff", receive)
        self.assertEqual(self.engine.click_source.alignment_ms, 125)
        spin_until(lambda: self.engine.position > 0.8 or self.errors)
        self.engine.stop()
        self.assertEqual(self.errors, [])
        self.assertGreater(max(peaks), 0.1)
        self.assertTrue(session.commit_take(take))
        self.assertLessEqual(max(abs(v) for v in pcm16(session.path(take))), 1)
        for guitar_only in (False, True):
            destination = self.root / f"export-{guitar_only}.wav"
            export_audio(session, destination, guitar_only=guitar_only)
            self.assertLessEqual(max(abs(v) for v in pcm16(destination)), 1)
        restored = MetronomeSettings.from_session(Session.open(session.folder).data)
        self.assertEqual(restored, MetronomeSettings(True, 240, 0.6, 125))
        self.assertEqual(MetronomeSettings.from_session({}), MetronomeSettings())


if __name__ == "__main__":
    unittest.main()
