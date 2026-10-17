"""Deletion keeps session metadata, audio files, and the transport consistent."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import wave

from gi.repository import GLib

from riff.audio import Device
from riff.storage import RATE, Session
from riff.ui import Window
from test_audio import spin_until
from test_playback_start import Transport


class TakeActions(Transport):
    delete_takes = Window.delete_takes
    apply_peaks = Window.apply_peaks

    def __init__(self, session, errors):
        super().__init__(session, errors)
        self._peaks_version = 0
        self.closed = False
        self.notices, self.refreshed = [], []

    def notice(self, message, error=False):
        self.notices.append((message, error))

    def refresh_tracks(self):
        self.refreshed.append(self.session.data.get("selected"))

    def refresh_takes(self):
        pass


class TakeDeletion(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = Session.new(self.root)
        backing = self.session.folder / "backing.wav"
        self.write_wave(backing, 2)
        self.session.data["backing"] = {"file": backing.name, "name": "Backing", "duration": 2}
        self.session.data.update(metronome_bpm=137, metronome_gain=1.5, alignment_ms=-10)
        self.takes = []
        for index in range(3):
            take = self.session.next_take()
            self.write_wave(self.session.path(take), 0.8 + index / 10, index + 1)
            self.session.commit_take(take)
            self.takes.append(take)
        self.errors = []
        self.app = TakeActions(self.session, self.errors)
        self.trash = self.root / "trash"
        self.trash.mkdir()
        self.files = patch("riff.ui.Gio.File.new_for_path", side_effect=self.trash_file)
        self.files.start()

    def tearDown(self):
        self.files.stop()
        self.app.engine.stop()
        self.temp.cleanup()

    def write_wave(self, path, duration, value=0):
        with wave.open(str(path), "wb") as audio:
            audio.setparams((2, 3, RATE, 0, "NONE", "not compressed"))
            audio.writeframes((value * 0x100000).to_bytes(3, "little", signed=True) * 2 * round(duration * RATE))

    def trash_file(self, path):
        archive = Path(path)
        return SimpleNamespace(trash=lambda _cancel: archive.replace(self.trash / archive.name))

    def assert_original_session(self):
        restored = Session.open(self.session.folder)
        self.assertEqual(restored.data["takes"], self.takes)
        self.assertEqual(restored.selected, self.takes[-1])
        self.assertTrue(all(self.session.path(take).is_file() for take in self.takes))
        self.assertFalse(list(self.session.folder.glob("deleted-takes-*")))

    def test_unselected_take_is_removed_without_recovery_or_duplicate_new_names(self):
        take = self.takes[1]
        original = self.session.path(take).read_bytes()
        archive = self.session.remove_takes([take["id"]])
        self.assertEqual((archive / take["file"]).read_bytes(), original)
        self.assertFalse(self.session.path(take).exists())
        restored = Session.open(self.session.folder)
        self.assertEqual(restored.data["takes"], [self.takes[0], self.takes[2]])
        self.assertEqual(restored.selected, self.takes[2])
        self.assertTrue(restored.path(restored.data["backing"]).is_file())
        self.assertNotIn(restored.next_take()["name"], {take["name"] for take in restored.data["takes"]})

    def test_selected_take_uses_next_then_previous_and_clear_preserves_backing_and_settings(self):
        self.session.data["selected"] = self.takes[1]["id"]
        self.session.remove_takes([self.takes[1]["id"]])
        self.assertEqual(self.session.selected, self.takes[2])
        self.session.remove_takes([self.takes[2]["id"]])
        self.assertEqual(self.session.selected, self.takes[0])
        self.session.remove_takes()
        restored = Session.open(self.session.folder)
        self.assertEqual(restored.data["takes"], [])
        self.assertIsNone(restored.selected)
        self.assertIsNone(restored.data["selected"])
        self.assertEqual(restored.data["metronome_bpm"], 137)
        self.assertEqual(restored.data["metronome_gain"], 1.5)
        self.assertEqual(restored.data["alignment_ms"], -10)
        self.assertTrue(restored.path(restored.data["backing"]).is_file())
        self.assertIsNone(restored.remove_takes())

    def test_save_failure_restores_files_and_selection(self):
        with patch.object(self.session, "save", side_effect=PermissionError("read-only metadata")):
            with self.assertRaises(PermissionError):
                self.session.remove_takes()
        self.assertEqual(self.session.data["takes"], self.takes)
        self.assert_original_session()

    def test_partial_move_failure_rolls_back_the_whole_batch(self):
        original_replace = Path.replace
        moved = []

        def replace(path, target):
            if path.parent == self.session.folder and path.name.startswith("take-"):
                if moved:
                    raise PermissionError("cannot move the second take")
                moved.append(path)
            return original_replace(path, target)

        with patch.object(Path, "replace", new=replace):
            with self.assertRaises(PermissionError):
                self.session.remove_takes()
        self.assertEqual(len(moved), 1)
        self.assert_original_session()

    def test_reopen_rolls_back_an_interrupted_deletion_before_metadata_was_saved(self):
        archive = self.session.folder / "deleted-takes-interrupted"
        archive.mkdir()
        for take in self.takes[:2]:
            self.session.path(take).replace(archive / take["file"])
        self.assert_original_session()

    def test_paths_cannot_delete_backing_or_escape_the_session(self):
        original = dict(self.takes[0])
        try:
            for filename in (self.session.data["backing"]["file"], "../outside.wav"):
                with self.subTest(filename=filename):
                    self.takes[0]["file"] = filename
                    with self.assertRaises(ValueError):
                        self.session.remove_takes([self.takes[0]["id"]])
        finally:
            self.takes[0].update(original)
        self.assert_original_session()

    def test_deleting_unselected_audio_keeps_playback_but_selected_audio_stops_it(self):
        self.app.play()
        pipeline = self.app.engine.pipeline
        self.app.delete_takes(self.takes[1]["id"])
        self.assertIs(self.app.engine.pipeline, pipeline)
        self.assertEqual(self.app.engine.mode, "play")
        self.assertEqual(self.session.selected, self.takes[2])
        self.app.delete_takes(self.takes[2]["id"])
        self.assertEqual(self.app.engine.mode, "idle")
        self.assertIsNone(self.app.engine.pipeline)
        self.assertEqual(self.session.selected, self.takes[0])
        self.assertEqual(self.app.refreshed, [self.takes[0]["id"]])
        self.assertEqual(Session.open(self.session.folder).data["takes"], [self.takes[0]])
        self.assertEqual(len(list(self.trash.glob("*/*.wav"))), 2)
        self.assertEqual(self.errors, [])

    def test_delete_all_stops_paused_playback_and_trashes_only_the_takes(self):
        self.app.play()
        self.app.engine.pause()
        self.app.delete_takes()
        self.assertEqual(self.app.engine.mode, "idle")
        self.assertEqual(self.session.data["takes"], [])
        self.assertIsNone(self.session.selected)
        self.assertEqual(len(list(self.trash.glob("*/*.wav"))), 3)
        self.assertTrue(self.session.path(self.session.data["backing"]).is_file())
        self.assertEqual(Session.open(self.session.folder).data["takes"], [])
        self.assertEqual(self.errors, [])

    def test_busy_and_recording_guards_keep_every_take(self):
        self.app.busy = True
        self.app.delete_takes()
        self.app.busy = False
        self.app.engine.capture(Device("test", "Silent input", 2), (0, 1), self.root / "pending.wav",
                                source_override="audiotestsrc is-live=true wave=silence")
        spin_until(lambda: self.app.engine.frames > RATE / 10 or self.errors)
        pipeline = self.app.engine.pipeline
        self.app.delete_takes(self.takes[0]["id"])
        self.app.delete_takes()
        self.assertIs(self.app.engine.pipeline, pipeline)
        self.assertEqual(self.app.engine.mode, "record")
        self.assert_original_session()
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_trash_failure_keeps_recoverable_audio_without_resurrecting_the_take(self):
        self.files.stop()
        with patch("riff.ui.Gio.File.new_for_path") as factory:
            factory.return_value.trash.side_effect = GLib.Error("Trash unavailable")
            self.app.delete_takes(self.takes[0]["id"])
        self.files.start()
        archives = list(self.session.folder.glob("deleted-takes-*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / self.takes[0]["file"]).is_file())
        self.assertEqual(Session.open(self.session.folder).data["takes"], self.takes[1:])
        self.assertIn("Trash unavailable", self.app.notices[-1][0])

    def test_outdated_waveform_errors_cannot_override_deletion_feedback(self):
        self.app._peaks_version = 3
        self.app.apply_peaks(None, [], 2, "audio already moved to Trash")
        self.assertEqual(self.app.notices, [])

    def test_deletion_moves_the_video_and_unfinished_video_with_the_audio(self):
        take = self.takes[0]
        video = self.session.path(take).with_suffix('.webm')
        partial = self.session.path(take).with_suffix('.webm.part')
        video.write_bytes(b'completed video')
        partial.write_bytes(b'unfinished video')
        self.session.attach_video(take)
        self.session.save()
        archive = self.session.remove_takes([take['id']])
        self.assertEqual({path.name for path in archive.iterdir()}, {take['file'], video.name, partial.name})
        self.assertEqual(Session.open(self.session.folder).data['takes'], self.takes[1:])

    def test_interrupted_deletion_restores_both_the_audio_and_video(self):
        take = self.takes[0]
        video = self.session.path(take).with_suffix('.webm')
        video.write_bytes(b'completed video')
        self.session.attach_video(take)
        self.session.save()
        archive = self.session.folder / 'deleted-takes-interrupted'
        archive.mkdir()
        for path in self.session.media_paths(take):
            path.replace(archive / path.name)
        self.assert_original_session()
        self.assertEqual(self.session.video_path(take).read_bytes(), b'completed video')

    def test_a_missing_video_does_not_prevent_opening_the_audio_session(self):
        take = self.takes[0]
        take.update(video='missing.webm', video_start=0.5)
        self.session.save()
        restored = Session.open(self.session.folder)
        self.assertNotIn('video', restored.data['takes'][0])
        self.assertNotIn('video_start', restored.data['takes'][0])
        self.assertTrue(restored.path(restored.data['takes'][0]).is_file())


if __name__ == "__main__":
    unittest.main()
