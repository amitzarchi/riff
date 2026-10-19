"""Real video/audio recordings from generated sources, without camera hardware."""

from array import array
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import wave

from gi.repository import GLib

from riff.audio import Device, Engine, Gst
from riff.metronome import MAX_GAIN, MetronomeSettings
from riff.storage import RATE, Session, export_video
from riff.webcam import BALANCED, HIGH, Camera, Playback, Preview, Recording
from test_audio import pcm16, spin_until


class WebcamIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = Session.new(self.root)
        self.errors = []
        self.engine = Engine(self.errors.append)

    def tearDown(self):
        self.engine.stop()
        self.temporary.cleanup()

    def backing(self, expression="0"):
        path = self.session.folder / "backing.wav"
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        f'aevalsrc={expression}|{expression}:s={RATE}:d=3', '-ac', '2', '-c:a', 'pcm_s24le', str(path)],
                       check=True, capture_output=True)
        self.session.data['backing'] = {'file': path.name, 'name': 'Backing', 'duration': 3}
        return path

    def record(self, backing=None, duration=0.65, position=0, silent=False, gain=1, metronome=None, source=None,
               quality=BALANCED, camera=None):
        take = self.session.next_take()
        self.session.data['playback_start'] = position
        video = Recording(camera or Camera('generated', 'Test camera'), self.session.path(take).with_suffix('.webm'),
                          gain, source_override=source or 'videotestsrc is-live=true pattern=ball',
                          preview_sink='fakesink name=camera_preview sync=false async=false', quality=quality)
        self.engine.capture(Device('test', 'Generated input', 2), (0, 1), self.session.path(take), backing,
                            source_override=('audiotestsrc is-live=true wave=silence' if silent else
                                             'audiotestsrc is-live=true wave=sine freq=880 volume=0.2'),
                            test_sink='fakesink name=monitor sync=true signal-handoffs=true',
                            position=position, metronome=metronome, video=video)
        spin_until(lambda: self.engine.frames >= duration * RATE or self.errors)
        self.engine.stop()
        self.assertEqual(self.errors, [])
        self.assertIsNone(self.engine.video_error)
        self.assertEqual(self.engine.video_path, video.path)
        self.assertGreater(self.engine.video_frames, 0)
        self.assertFalse(video.temporary.exists())
        take['video_start'] = position
        self.session.commit_take(take)
        return take, video.path

    def probe(self, path):
        raw = subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                       '-of', 'json', str(path)])
        return json.loads(raw)

    def video_audio(self, path):
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                                       '-ac', '2', '-ar', str(RATE), '-f', 'f32le', 'pipe:1'])
        return array('f', raw)

    def tone(self, values, frequency):
        values = values[round(0.15 * RATE) * 2:round(0.5 * RATE) * 2:2]
        angle = 2 * math.pi * frequency / RATE
        real = sum(value * math.cos(index * angle) for index, value in enumerate(values))
        imaginary = sum(value * math.sin(index * angle) for index, value in enumerate(values))
        return 2 * math.hypot(real, imaginary) / len(values)

    def test_video_has_playable_frames_and_the_guitar_backing_mix_at_the_capture_duration(self):
        take, video = self.record(self.backing('0.2*sin(2*PI*220*t)'), gain=0.5)
        probe = self.probe(video)
        streams = {stream['codec_type']: stream for stream in probe['streams']}
        self.assertEqual(streams['video']['codec_name'], 'vp8')
        self.assertEqual((streams['video']['width'], streams['video']['height']), (1280, 720))
        self.assertEqual(streams['video']['r_frame_rate'], '30/1')
        self.assertEqual(streams['audio']['codec_name'], 'opus')
        self.assertEqual(streams['audio']['channels'], 2)
        elapsed = self.engine.frames / RATE
        self.assertAlmostEqual(float(probe['format']['duration']), elapsed, delta=0.07)
        values = self.video_audio(video)
        self.assertAlmostEqual(len(values) / 2 / RATE, elapsed, delta=0.04)
        self.assertAlmostEqual(self.tone(values, 220), 0.2 * 0.65, delta=0.02)
        self.assertAlmostEqual(self.tone(values, 880), 0.2 * 0.5, delta=0.02)
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(video), '-frames:v', '1',
                                       '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'])
        self.assertEqual(len(raw), 1280 * 720)
        self.assertGreater(max(raw) - min(raw), 150)
        self.assertEqual(Session.open(self.session.folder).selected['video'], video.name)
        self.assertEqual(take['video_start'], 0)

    def test_metronome_stays_out_of_video_as_well_as_the_raw_take(self):
        peaks = []
        real_capture = self.engine.capture

        def capture(*args, **kwargs):
            real_capture(*args, **kwargs)

            def receive(_sink, buffer, _pad):
                values = array('f', buffer.extract_dup(0, buffer.get_size()))
                peaks.append(max(map(abs, values)))

            self.engine.pipeline.get_by_name('monitor').connect('handoff', receive)

        self.engine.capture = capture
        take, video = self.record(self.backing(), silent=True,
                                  metronome=MetronomeSettings(True, 240, MAX_GAIN))
        self.assertGreater(max(peaks), 0.8)
        self.assertLessEqual(max(map(abs, pcm16(self.session.path(take)))), 1)
        self.assertLess(max(map(abs, self.video_audio(video))), 0.0001)

    def test_start_marker_trims_the_video_mix_without_a_long_video_intro(self):
        backing = self.backing('if(lt(t\\,0.5)\\,0.2*sin(2*PI*220*t)\\,0.2*sin(2*PI*440*t))')
        take, video = self.record(backing, position=0.625, silent=True)
        values = self.video_audio(video)
        self.assertAlmostEqual(self.tone(values, 440), 0.13, delta=0.02)
        self.assertLess(self.tone(values, 220), 0.01)
        self.assertLess(float(self.probe(video)['format']['duration']), 0.85)
        self.assertEqual(take['video_start'], 0.625)
        self.assertEqual(max(map(abs, pcm16(self.session.path(take))[:round(0.625 * RATE) * 2])), 0)

    def test_camera_also_records_without_a_backing_track(self):
        take, video = self.record()
        self.assertGreater(self.tone(self.video_audio(video), 880), 0.17)
        self.assertEqual(Session.open(self.session.folder).selected['video'], video.name)
        self.assertGreater(take['duration'], 0.6)

    def test_high_records_native_1080p_jpeg_and_exports_the_full_resolution_clip(self):
        camera = Camera('generated', 'HD camera',
                        'video/x-raw,width=1280,height=720,framerate=30/1;'
                        'image/jpeg,width=1920,height=1080,framerate=30/1')
        take, video = self.record(self.backing('0.2*sin(2*PI*220*t)'), quality=HIGH, camera=camera,
                                  source='videotestsrc is-live=true pattern=ball ! jpegenc')
        probe = self.probe(video)
        picture = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
        self.assertEqual((picture['width'], picture['height'], picture['r_frame_rate']), (1920, 1080, '30/1'))
        with wave.open(str(self.session.path(take))) as audio:
            self.assertEqual((audio.getsampwidth(), audio.getframerate(), audio.getnchannels()), (3, RATE, 2))
        output = self.root / 'high.mp4'
        export_video(self.session, output)
        exported = self.probe(output)
        picture = next(stream for stream in exported['streams'] if stream['codec_type'] == 'video')
        self.assertEqual((picture['width'], picture['height'], picture['r_frame_rate']), (1920, 1080, '30/1'))
        self.assertAlmostEqual(float(exported['format']['duration']), float(probe['format']['duration']), delta=0.04)
        values = self.video_audio(output)
        self.assertAlmostEqual(self.tone(values, 880), 0.2, delta=0.02)
        self.assertAlmostEqual(self.tone(values, 220), 0.13, delta=0.02)

    def test_high_falls_back_to_a_smaller_native_camera_without_upscaling(self):
        camera = Camera('generated', 'Small camera', 'video/x-raw,width=640,height=480,framerate=15/1')
        _, video = self.record(quality=HIGH, camera=camera)
        picture = next(stream for stream in self.probe(video)['streams'] if stream['codec_type'] == 'video')
        self.assertEqual((picture['width'], picture['height'], picture['r_frame_rate']), (640, 480, '15/1'))

    def test_camera_failure_preserves_the_audio_take(self):
        take = self.session.next_take()
        video = Recording(Camera('generated', 'Broken test camera'), self.session.path(take).with_suffix('.webm'),
                          source_override='videotestsrc is-live=true ! identity error-after=12',
                          preview_sink='fakesink name=camera_preview sync=false async=false')
        self.engine.capture(Device('test', 'Generated input', 2), (0, 1), self.session.path(take),
                            source_override='audiotestsrc is-live=true wave=sine volume=0.1', video=video)
        spin_until(lambda: self.errors)
        self.engine.stop()
        self.assertTrue(self.engine.video_error)
        self.assertIsNone(self.engine.video_path)
        self.assertTrue(self.session.commit_take(take))
        self.assertGreater(take['duration'], 0.1)
        self.assertNotIn('video', Session.open(self.session.folder).selected)

    def test_video_is_attached_when_recovering_an_interrupted_uncommitted_take(self):
        take, video = self.record(position=0.5)
        self.session.data['takes'] = []
        self.session.data['selected'] = None
        self.session.save()
        restored = Session.open(self.session.folder)
        self.assertEqual(restored.selected['file'], take['file'])
        self.assertEqual(restored.selected['video'], video.name)
        self.assertEqual(restored.selected['video_start'], 0.5)

    def test_preview_releases_the_old_camera_when_switching_and_stopping(self):
        preview = Preview(self.errors.append)
        try:
            for name in ('first', 'second'):
                previous = preview.pipeline
                preview.start(Camera(name, name), source_override='videotestsrc is-live=true',
                              test_sink='fakesink sync=false async=false')
                if previous:
                    self.assertEqual(previous.get_state(Gst.SECOND)[1], Gst.State.NULL)
                self.assertIsNotNone(preview.pipeline)
                time.sleep(0.04)
            preview.stop()
            self.assertIsNone(preview.pipeline)
            self.assertEqual(self.errors, [])
        finally:
            preview.stop()

    def test_take_video_follows_audio_pause_seek_and_rewind(self):
        backing = self.backing()
        take, video = self.record(backing, duration=1)
        player = Playback(self.errors.append)
        timer = None
        try:
            self.engine.play(backing, self.session.path(take), None, test_sink='fakesink sync=true')
            player.start(video, self.engine.pipeline.get_clock(), test_sink='fakesink name=camera_preview sync=true')

            def sync():
                player.sync(self.engine.position, 0, self.engine.mode)
                return True

            timer = GLib.timeout_add(20, sync)
            spin_until(lambda: self.engine.position > 0.2 or self.errors)
            self.assertAlmostEqual(player.position, self.engine.position, delta=0.06)
            self.assertTrue(player.playing)
            self.engine.pause()
            player.sync(self.engine.position, 0, self.engine.mode, force=True)
            player.pipeline.get_state(Gst.SECOND)
            paused = player.position
            time.sleep(0.08)
            self.assertAlmostEqual(player.position, paused, delta=0.015)
            self.assertFalse(player.playing)
            self.assertTrue(self.engine.seek(0.55))
            player.sync(0.55, 0, 'pause', force=True)
            player.pipeline.get_state(Gst.SECOND)
            self.assertAlmostEqual(player.position, 0.55, delta=0.035)
            self.engine.pause()
            player.sync(self.engine.position, 0, 'play')
            spin_until(lambda: self.engine.position > 0.65 or self.errors)
            self.assertAlmostEqual(player.position, self.engine.position, delta=0.06)
            pipeline = player.pipeline
            self.assertTrue(self.engine.seek(0.05))
            player.sync(0.05, 0, 'play', force=True)
            spin_until(lambda: 0.08 < player.position < 0.3 or self.errors)
            self.assertIs(player.pipeline, pipeline)
            self.assertEqual(self.errors, [])
        finally:
            if timer:
                GLib.source_remove(timer)
            player.stop()

    def test_video_start_alignment_end_and_restart_do_not_change_audio_duration(self):
        backing = self.backing()
        take, video = self.record(backing, duration=0.65, position=0.625)
        player = Playback(self.errors.append)
        timer = None
        start = 0.625 - 0.125
        try:
            self.engine.play(backing, self.session.path(take), None, alignment_ms=-125,
                             test_sink='fakesink sync=true')
            player.start(video, self.engine.pipeline.get_clock(), test_sink='fakesink name=camera_preview sync=true')
            self.assertEqual(player.sync(0.1, start, 'play'), 'before')
            self.assertFalse(player.playing)
            self.assertLess(player.position, 0.04)

            def sync():
                player.sync(self.engine.position, start, self.engine.mode)
                return True

            timer = GLib.timeout_add(20, sync)
            spin_until(lambda: self.engine.position > start + 0.2 or self.errors)
            self.assertAlmostEqual(player.position, self.engine.position - start, delta=0.06)
            spin_until(lambda: self.engine.position > start + player.duration + 0.06 or self.errors)
            self.assertEqual(player.sync(self.engine.position, start, 'play'), 'after')
            self.assertEqual(self.engine.mode, 'play')
            self.assertFalse(player.playing)
            self.assertTrue(self.engine.seek(start + 0.1))
            player.sync(start + 0.1, start, 'play', force=True)
            spin_until(lambda: player.playing and player.position > 0.12 or self.errors)
            self.assertAlmostEqual(player.position, self.engine.position - start, delta=0.06)
            self.assertEqual(self.errors, [])
        finally:
            if timer:
                GLib.source_remove(timer)
            player.stop()


if __name__ == '__main__':
    unittest.main()
