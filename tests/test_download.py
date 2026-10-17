"""YouTube import behavior with a fake downloader and real FFmpeg conversion."""

import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from riff.download import DownloadCancelled, YouTubeDownload, youtube_url
from riff.storage import Session, read_peaks, run_ffmpeg


class YouTubeImport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = Session.new(self.root)
        self.source = self.root / "download.m4a"
        run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.2",
                    "-c:a", "aac", str(self.source)])
        self.executable = self.root / "yt-dlp"
        self.arguments = self.root / "arguments.json"

    def tearDown(self):
        self.temp.cleanup()

    def fake_downloader(self, mode="success"):
        self.executable.write_text(
            "#!/usr/bin/python\n"
            "import json,pathlib,shutil,sys,time\n"
            f"pathlib.Path({str(self.arguments)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "target=pathlib.Path(sys.argv[sys.argv.index('--output')+1].replace('%(ext)s','m4a'))\n"
            "target.with_suffix('.part').write_bytes(b'partial download')\n"
            + ("print('ERROR: Video unavailable',flush=True);sys.exit(1)\n" if mode == "error" else
               "print('RIFF_TITLE '+json.dumps('Blues backing track'),flush=True)\n"
               "print('RIFF_PROGRESS 25 100',flush=True)\n"
               + ("time.sleep(20)\n" if mode == "slow" else "")
               + f"shutil.copyfile({str(self.source)!r},target)\n"
               + "print('RIFF_PROGRESS 100 100',flush=True)\n"
               + "print('RIFF_FILE '+json.dumps(str(target)),flush=True)\n")
        )
        self.executable.chmod(0o755)
        return patch("riff.download.shutil.which", return_value=str(self.executable))

    def test_normalize_video_urls_without_playlist_or_tracking(self):
        expected = "https://www.youtube.com/watch?v=BaW_jenozKc"
        for link in (
            "https://youtu.be/BaW_jenozKc?si=abc", " youtube.com/watch?v=BaW_jenozKc&list=xyz&t=30 ",
            "https://m.youtube.com/watch?v=BaW_jenozKc", "https://music.youtube.com/watch?v=BaW_jenozKc",
            "https://www.youtube.com/shorts/BaW_jenozKc", "https://www.youtube.com/live/BaW_jenozKc",
            "https://www.youtube-nocookie.com/embed/BaW_jenozKc",
        ):
            with self.subTest(link=link):
                self.assertEqual(youtube_url(link), expected)
        for link in ("", "https://example.com/watch?v=BaW_jenozKc", "https://youtube.com.evil.test/watch?v=BaW_jenozKc",
                     "https://youtube.com/playlist?list=xyz", "file:///tmp/a.wav", "https://youtube.com/@channel",
                     "https://youtube.com/watch?v=short", "https://user:password@youtube.com/watch?v=BaW_jenozKc"):
            with self.subTest(link=link), self.assertRaises(ValueError):
                youtube_url(link)

    def test_download_normalizes_audio_and_preserves_source(self):
        progress = []
        with self.fake_downloader():
            item = YouTubeDownload("https://youtu.be/BaW_jenozKc?list=xyz", self.session,
                                   lambda text, fraction: progress.append((text, fraction))).run()
        with wave.open(str(self.session.path(item))) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (2, 3, 48000))
        self.assertGreater(max(read_peaks(self.session.path(item))), 0.05)
        self.assertEqual(item["name"], "Blues backing track")
        self.assertEqual(item["source_url"], "https://www.youtube.com/watch?v=BaW_jenozKc")
        self.assertIn(("Downloading audio…", 0.25), progress)
        args = json.loads(self.arguments.read_text())
        self.assertEqual(args[-2:], ["--", item["source_url"]])
        self.assertIn("--ignore-config", args)
        self.assertIn("--no-playlist", args)
        self.assertEqual(list(self.session.folder.glob(".youtube-*")), [])
        self.assertIsNone(self.session.data["backing"])
        self.session.data["backing"] = item
        self.session.save()
        self.assertEqual(Session.open(self.session.folder).data["backing"]["source_url"], item["source_url"])

    def test_failure_keeps_current_backing_and_removes_partial_files(self):
        self.session.data["backing"] = {"file": "old.wav", "name": "Old track", "duration": 1}
        original = self.session.data["backing"].copy()
        with self.fake_downloader("error"), self.assertRaisesRegex(RuntimeError, "Video unavailable"):
            YouTubeDownload("https://youtu.be/BaW_jenozKc", self.session).run()
        self.assertEqual(self.session.data["backing"], original)
        self.assertEqual(list(self.session.folder.iterdir()), [self.session.folder / "session.json"])

    def test_cancel_stops_downloader_and_removes_partial_files(self):
        job = YouTubeDownload("https://youtu.be/BaW_jenozKc", self.session)
        job.progress = lambda _text, fraction: job.cancel() if fraction is not None else None
        with self.fake_downloader("slow"), self.assertRaises(DownloadCancelled):
            job.run()
        self.assertIsNone(job._process)
        self.assertEqual(list(self.session.folder.iterdir()), [self.session.folder / "session.json"])

    def test_cancel_during_conversion_removes_new_backing(self):
        job = YouTubeDownload("https://youtu.be/BaW_jenozKc", self.session)
        job.progress = lambda text, _fraction: job.cancel() if text == "Preparing session audio…" else None
        with self.fake_downloader(), self.assertRaises(DownloadCancelled):
            job.run()
        self.assertEqual(list(self.session.folder.iterdir()), [self.session.folder / "session.json"])

    def test_missing_downloader_has_actionable_error(self):
        with patch("riff.download.shutil.which", return_value=None), self.assertRaisesRegex(RuntimeError, "not installed"):
            YouTubeDownload("https://youtu.be/BaW_jenozKc", self.session).run()
