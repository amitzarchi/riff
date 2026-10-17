"""Decode real MP4 exports and check the picture and current session mix."""

from array import array
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from riff.storage import RATE, Session, export_audio, export_video, run_ffmpeg


class VideoExport(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = Session.new(self.root)
        backing = self.session.folder / "backing.wav"
        run_ffmpeg(["-f", "lavfi", "-i",
                    f"aevalsrc=0.15*sin(2*PI*if(lt(t\\,0.8)\\,220\\,330)*t):s={RATE}:d=1.6",
                    "-ac", "2", "-c:a", "pcm_s24le", str(backing)])
        self.session.data.update(backing={"file": backing.name, "name": "Backing", "duration": 1.6},
                                 backing_gain=0.25, guitar_gain=0.5,
                                 metronome_enabled=True, metronome_gain=2)
        self.take = self.session.next_take()
        self.take["video_start"] = 0.4
        run_ffmpeg(["-f", "lavfi", "-i",
                    f"aevalsrc=if(gte(t\\,0.4)\\,0.2*sin(2*PI*880*(t-0.4))\\,0):s={RATE}:d=1",
                    "-ac", "2", "-c:a", "pcm_s24le", str(self.session.path(self.take))])
        video = self.session.path(self.take).with_suffix(".webm")
        run_ffmpeg(["-f", "lavfi", "-i",
                    "color=c=red:s=160x90:r=30:d=0.6,drawbox=color=blue:t=fill:enable='gte(t,0.3)'",
                    "-f", "lavfi", "-i", f"aevalsrc=0.8*sin(2*PI*440*t):s={RATE}:d=0.6",
                    "-c:v", "libvpx", "-deadline", "realtime", "-c:a", "libopus", str(video)])
        self.session.commit_take(self.take)
        self.output = self.root / "performance.mp4"

    def tearDown(self):
        self.temporary.cleanup()

    def probe(self):
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(self.output)]))

    def audio(self):
        return array("f", subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(self.output), "-map", "0:a:0",
            "-ar", str(RATE), "-ac", "1", "-f", "f32le", "pipe:1"]))

    def tone(self, values, frequency, start=0.1, stop=0.3):
        values = values[round(start * RATE):round(stop * RATE)]
        angle = 2 * math.pi * frequency / RATE
        real = sum(value * math.cos(index * angle) for index, value in enumerate(values))
        imaginary = sum(value * math.sin(index * angle) for index, value in enumerate(values))
        return 2 * math.hypot(real, imaginary) / len(values)

    def frame(self, at):
        return subprocess.check_output([
            "ffmpeg", "-v", "error", "-ss", str(at), "-i", str(self.output),
            "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"])

    def assert_color(self, at, channel):
        frame = self.frame(at)
        self.assertGreater(frame[channel], 200)
        self.assertLess(max(value for index, value in enumerate(frame) if index != channel), 20)

    def test_mp4_uses_current_mix_trimmed_to_video_instead_of_recorded_audio(self):
        originals = {path: path.read_bytes() for path in self.session.media_paths(self.take)}
        export_video(self.session, self.output)
        probe = self.probe()
        streams = {stream["codec_type"]: stream for stream in probe["streams"]}
        self.assertEqual(set(streams), {"audio", "video"})
        self.assertEqual(streams["video"]["codec_name"], "h264")
        self.assertEqual(streams["video"]["pix_fmt"], "yuv420p")
        self.assertEqual(streams["audio"]["codec_name"], "aac")
        self.assertEqual(streams["audio"]["channels"], 2)
        self.assertEqual(streams["audio"]["sample_rate"], str(RATE))
        self.assertAlmostEqual(float(probe["format"]["duration"]), 0.6, delta=0.025)
        self.assertAlmostEqual(float(streams["video"]["duration"]), 0.6, delta=0.01)
        self.assertAlmostEqual(len(self.audio()) / RATE, 0.6, delta=0.025)
        values = self.audio()
        self.assertAlmostEqual(self.tone(values, 220), 0.15 * 0.25, delta=0.005)
        self.assertAlmostEqual(self.tone(values, 880), 0.2 * 0.5, delta=0.008)
        self.assertLess(self.tone(values, 440), 0.003)
        rms = math.sqrt(sum(v * v for v in values[int(0.1 * RATE):int(0.3 * RATE)]) / (0.2 * RATE))
        self.assertAlmostEqual(rms, math.sqrt((0.0375 ** 2 + 0.1 ** 2) / 2), delta=0.005)
        self.assert_color(0.1, 0)
        self.assert_color(0.4, 2)
        self.assertAlmostEqual(self.tone(values, 330, 0.45, 0.55), 0.0375, delta=0.005)
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(list(self.root.glob(".riff-export-*")), [])

    def test_positive_alignment_keeps_full_clip_and_seeks_backing_to_the_aligned_start(self):
        self.session.data["alignment_ms"] = 200
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assert_color(0.1, 0)
        self.assert_color(0.4, 2)
        values = self.audio()
        self.assertAlmostEqual(self.tone(values, 880), 0.1, delta=0.008)
        self.assertAlmostEqual(self.tone(values, 220, 0.05, 0.15), 0.0375, delta=0.005)
        self.assertAlmostEqual(self.tone(values, 330, 0.3, 0.5), 0.0375, delta=0.005)

    def test_negative_alignment_keeps_full_clip_and_delays_backing_until_session_zero(self):
        self.session.data["alignment_ms"] = -600
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assert_color(0.1, 0)
        self.assert_color(0.4, 2)
        values = self.audio()
        self.assertAlmostEqual(self.tone(values, 880, 0.1, 0.3), 0.1, delta=0.008)
        self.assertLess(self.tone(values, 220, 0.05, 0.15), 0.003)
        self.assertAlmostEqual(self.tone(values, 220, 0.3, 0.5), 0.0375, delta=0.005)

    def test_clip_shifted_entirely_before_backing_keeps_video_and_guitar(self):
        self.session.data["alignment_ms"] = -2000
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assert_color(0.1, 0)
        self.assert_color(0.4, 2)
        values = self.audio()
        self.assertAlmostEqual(self.tone(values, 880), 0.1, delta=0.008)
        self.assertLess(self.tone(values, 220), 0.003)

    def test_video_without_backing_and_optional_wav_exports(self):
        self.session.data["backing"] = None
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assert_color(0.1, 0)
        self.assert_color(0.4, 2)
        for guitar_only in (False, True):
            output = self.root / f"audio-{guitar_only}.wav"
            export_audio(self.session, output, guitar_only=guitar_only)
            with wave.open(str(output)) as audio:
                self.assertEqual((audio.getsampwidth(), audio.getframerate(), audio.getnchannels()), (3, RATE, 2))

    def test_failed_encoding_preserves_existing_export_and_removes_partial_files(self):
        self.output.write_bytes(b"Previous successful export")

        def fail(args):
            if "libx264" in args:
                Path(args[-1]).write_bytes(b"Incomplete video")
                raise RuntimeError("Encoder failed")
            return run_ffmpeg(args)

        with patch("riff.storage.run_ffmpeg", side_effect=fail), self.assertRaisesRegex(RuntimeError, "Encoder failed"):
            export_video(self.session, self.output)
        self.assertEqual(self.output.read_bytes(), b"Previous successful export")
        self.assertEqual(list(self.root.glob(".riff-export-*")), [])

    def test_missing_video_and_session_files_are_protected(self):
        protected = [self.session.folder / "session.json", self.session.path(self.session.data["backing"]),
                     *self.session.media_paths(self.take)]
        for path in protected:
            original = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "new export filename"):
                export_video(self.session, path)
            self.assertEqual(path.read_bytes(), original)
        self.session.video_path(self.take).unlink()
        with self.assertRaisesRegex(ValueError, "no video"):
            export_video(self.session, self.output)
        self.assertFalse(self.output.exists())

    def test_muted_mix_still_exports_the_full_video_with_silent_audio(self):
        self.session.data.update(backing=None, guitar_gain=0, alignment_ms=-2000)
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assertLess(max(map(abs, self.audio())), 0.0001)
        self.assertEqual(list(self.root.glob(".riff-export-*")), [])

    def test_audio_ending_before_video_is_padded_to_the_clip_end(self):
        self.session.data["backing"] = None
        path = self.session.path(self.take)
        with wave.open(str(path)) as audio:
            params = audio.getparams()
            shorter = audio.readframes(round(0.6 * RATE))
        with wave.open(str(path), "wb") as audio:
            audio.setparams(params)
            audio.writeframes(shorter)
        export_video(self.session, self.output)
        self.assertAlmostEqual(float(self.probe()["format"]["duration"]), 0.6, delta=0.025)
        self.assert_color(0.4, 2)
        values = self.audio()
        self.assertAlmostEqual(self.tone(values, 880, 0.05, 0.15), 0.1, delta=0.008)
        self.assertLess(max(map(abs, values[round(0.35 * RATE):round(0.55 * RATE)])), 0.001)


if __name__ == "__main__":
    unittest.main()
