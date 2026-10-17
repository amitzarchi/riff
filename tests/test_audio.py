"""End-to-end audio checks using generated signals, never a live microphone."""

import tempfile
import time
import unittest
import wave
from array import array
from pathlib import Path

from gi.repository import GLib

from riff.audio import Device, Engine
from riff.storage import RATE, Session, export_audio, import_backing, open_take, read_peaks, run_ffmpeg


def spin_until(predicate, seconds=5):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.005)
    if not predicate():
        raise AssertionError("Audio operation timed out")


def pcm16(path):
    with wave.open(str(path)) as audio:
        width = audio.getsampwidth()
        raw = audio.readframes(audio.getnframes())
    if width == 3:
        raw = b"".join(raw[i + 1:i + 3] for i in range(0, len(raw), 3))
    return array("h", raw)


class AudioIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = Session.new(self.root)
        self.errors = []
        self.ended = False
        self.engine = Engine(self.errors.append, self.end)

    def tearDown(self):
        self.engine.stop()
        self.temp.cleanup()

    def end(self):
        self.ended = True

    def signal_file(self, name, frequencies, duration=1):
        destination = self.root / name
        source = "|".join(f"0.15*sin(2*PI*{freq}*t)" for freq in frequencies)
        run_ffmpeg(["-f", "lavfi", "-i", f"aevalsrc={source}:s={RATE}:d={duration}",
                    "-c:a", "pcm_s24le", str(destination)])
        return destination

    def test_selected_usb_channels_survive_capture(self):
        source = self.signal_file("four-channel.wav", [220, 330, 880, 1100])
        take = self.session.next_take()
        self.engine.capture(Device("test", "Test interface", 4), (2, 3), self.session.path(take),
                            source_override=f'filesrc location="{source}" ! wavparse')
        spin_until(lambda: self.ended or self.errors)
        self.engine.stop()
        self.assertEqual(self.errors, [])
        self.session.commit_take(take)
        samples = pcm16(self.session.path(take))
        for channel, expected in enumerate((880, 1100)):
            signal = samples[channel::2]
            crossings = sum(a <= 0 < b for a, b in zip(signal, signal[1:]))
            self.assertAlmostEqual(crossings / (len(signal) / RATE), expected, delta=2)
        self.assertAlmostEqual(take["duration"], 1, delta=0.03)
        self.assertGreater(max(read_peaks(self.session.path(take))), 0.1)
        restored = Session.open(self.session.folder)
        self.assertEqual(restored.selected["id"], take["id"])

    def test_import_mix_export_and_alignment(self):
        backing = self.signal_file("backing.wav", [220, 220], 1.3)
        guitar = self.signal_file("guitar.wav", [880, 1100], 1)
        self.session.data["backing"] = import_backing(backing, self.session)
        take = self.session.next_take()
        self.session.path(take).write_bytes(guitar.read_bytes())
        self.session.commit_take(take)
        mixed = self.root / "mix.wav"
        export_audio(self.session, mixed)
        with wave.open(str(mixed)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (2, 3, RATE))
            self.assertAlmostEqual(audio.getnframes() / RATE, 1.3, delta=0.03)
        self.session.data["alignment_ms"] = 100
        delayed = self.root / "delayed.wav"
        export_audio(self.session, delayed, guitar_only=True)
        samples = pcm16(delayed)
        self.assertLess(max(abs(v) for v in samples[:int(0.095 * RATE) * 2]), 4)
        self.assertGreater(max(abs(v) for v in samples[int(0.11 * RATE) * 2:]), 100)
        self.session.data["alignment_ms"] = -100
        trimmed = self.root / "trimmed.wav"
        export_audio(self.session, trimmed, guitar_only=True)
        with wave.open(str(trimmed)) as audio:
            self.assertAlmostEqual(audio.getnframes() / RATE, 0.9, delta=0.02)

    def test_play_pause_seek_and_end(self):
        backing = self.signal_file("play.wav", [220, 220], 2)
        self.engine.play(backing, None, None, test_sink="fakesink sync=true")
        spin_until(lambda: self.engine.position > 0.1 or self.errors)
        self.engine.pause()
        position = self.engine.position
        time.sleep(0.06)
        self.assertAlmostEqual(self.engine.position, position, delta=0.02)
        self.assertTrue(self.engine.seek(1.7))
        self.engine.pause()
        spin_until(lambda: self.ended or self.errors)
        self.assertEqual(self.errors, [])

    def test_live_capture_and_backing_share_pipeline(self):
        backing = self.signal_file("bed.wav", [220, 220], 0.3)
        take = self.session.next_take()
        self.engine.capture(Device("test", "Test interface", 2), (0, 1), self.session.path(take),
                            backing=backing, source_override="audiotestsrc is-live=true wave=sine volume=0.1",
                            test_sink="fakesink sync=true")
        spin_until(lambda: self.engine.position > 0.4 or self.errors)
        self.engine.stop()
        self.assertEqual(self.errors, [])
        self.assertTrue(self.session.commit_take(take))
        self.assertGreater(take["duration"], 0.3)

    def test_playback_alignment_matches_export(self):
        guitar = self.signal_file("aligned.wav", [880, 1100], 0.5)
        for offset in (100, -100):
            with self.subTest(offset=offset):
                self.ended = False
                self.engine.play(None, guitar, None, alignment_ms=offset,
                                 test_sink="appsink name=render sync=false max-buffers=200")
                sink = self.engine.pipeline.get_by_name("render")
                rendered = array("f")
                while sample := sink.emit("try-pull-sample", 2_000_000_000):
                    buf = sample.get_buffer()
                    rendered.frombytes(buf.extract_dup(0, buf.get_size()))
                spin_until(lambda: self.ended or self.errors)
                self.assertEqual(self.errors, [])
                self.assertAlmostEqual(len(rendered) / 2 / RATE, 0.5 + offset / 1000, delta=0.02)
                if offset > 0:
                    self.assertLess(max(abs(v) for v in rendered[:int(0.095 * RATE) * 2]), 0.001)
                self.engine.stop()

    def test_uncommitted_take_is_recovered(self):
        source = self.signal_file("recover.wav", [440, 440], 0.2)
        take = self.session.next_take()
        self.session.path(take).write_bytes(source.read_bytes())
        reopened = Session.open(self.session.folder)
        self.assertEqual(reopened.selected["file"], take["file"])
        self.assertAlmostEqual(reopened.selected["duration"], 0.2, delta=0.01)
        self.assertEqual(len(Session.open(self.session.folder).data["takes"]), 1)

    def test_recording_from_offset_seeks_backing_and_aligns_saved_guitar(self):
        backing = self.signal_file("backing-offset.wav", [220, 220], 1.2)
        incoming = self.signal_file("incoming.wav", [880, 1100], 0.3)
        take = self.session.next_take()
        position = 0.625
        self.engine.capture(Device("test", "Test input", 2), (0, 1), self.session.path(take),
                            backing=backing, backing_gain=1, position=position,
                            source_override=f'filesrc location="{incoming}" ! wavparse',
                            test_sink="appsink name=render sync=false max-buffers=200")
        sink = self.engine.pipeline.get_by_name("render")
        rendered = array("f")
        while sample := sink.emit("try-pull-sample", 2_000_000_000):
            buffer = sample.get_buffer()
            rendered.frombytes(buffer.extract_dup(0, buffer.get_size()))
        spin_until(lambda: self.ended or self.errors)
        self.assertEqual(self.errors, [])
        self.assertAlmostEqual(self.engine.position, 0.925, delta=0.001)
        self.engine.stop()
        expected = pcm16(backing)[round(position * RATE) * 2:]
        self.assertEqual(len(rendered), len(expected))
        self.assertLess(max(abs(actual - wanted / 32768) for actual, wanted in zip(rendered, expected)), 0.00005)
        self.session.commit_take(take)
        saved = pcm16(self.session.path(take))
        prefix = round(position * RATE) * 2
        self.assertEqual(max(abs(value) for value in saved[:prefix]), 0)
        self.assertEqual(saved[prefix:], pcm16(incoming))
        destination = self.root / "offset-guitar-export.wav"
        export_audio(self.session, destination, guitar_only=True)
        exported = pcm16(destination)
        self.assertLessEqual(max(abs(value) for value in exported[:prefix]), 1)
        self.assertGreater(max(abs(value) for value in exported[prefix:]), 100)
        self.assertAlmostEqual(take["duration"], 0.925, delta=0.001)

    def test_long_start_creates_sparse_silence_and_recovers_on_the_timeline(self):
        take = self.session.next_take()
        path = self.session.path(take)
        start_frames = 1200 * RATE
        writer, output = open_take(path, start_frames)
        try:
            writer.writeframes(b"\x01\0\0" * 2 * 100)
        finally:
            writer.close()
            output.close()
        with wave.open(str(path)) as audio:
            self.assertEqual(audio.getnframes(), start_frames + 100)
            self.assertEqual(audio.readframes(100), bytes(600))
            audio.setpos(start_frames)
            self.assertEqual(audio.readframes(100), b"\x01\0\0" * 2 * 100)
        self.assertGreater(path.stat().st_size, 300_000_000)
        self.assertLess(path.stat().st_blocks * 512, 1_000_000)
        restored = Session.open(self.session.folder)
        self.assertAlmostEqual(restored.selected["duration"], 1200 + 100 / RATE, delta=0.0001)


if __name__ == "__main__":
    unittest.main()
