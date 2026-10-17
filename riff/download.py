"""Cancellable, single-video YouTube imports through the installed yt-dlp CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import Session, import_backing


class DownloadCancelled(Exception):
    pass


def youtube_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if host in ("youtu.be", "www.youtu.be") and len(parts) == 1:
            video = parts[0]
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
                      "youtube-nocookie.com", "www.youtube-nocookie.com"):
            if parts == ["watch"]:
                video = parse_qs(parsed.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
                video = parts[1]
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
            raise ValueError
    except ValueError:
        raise ValueError("Paste a YouTube video link, such as youtube.com/watch?v=… or youtu.be/….") from None
    # Strip playlist, tracking and time parameters so one complete video is imported.
    return f"https://www.youtube.com/watch?v={video}"


class YouTubeDownload:
    def __init__(self, url: str, session: Session, progress=lambda message, fraction: None):
        self.url = youtube_url(url)
        self.session, self.progress = session, progress
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            process = self._process
        if process:
            self._signal(process, signal.SIGTERM)

            def ensure_stopped():
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._signal(process, signal.SIGKILL)

            threading.Thread(target=ensure_stopped, daemon=True).start()

    @staticmethod
    def _signal(process, sig):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def _check_cancelled(self):
        if self.cancelled.is_set():
            raise DownloadCancelled()

    def _run(self, arguments: list[str], on_line=lambda line: None):
        tail = deque(maxlen=12)
        with self._lock:
            self._check_cancelled()
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                       start_new_session=True)
            self._process = process
        try:
            for line in process.stdout:
                line = line.strip()
                tail.append(line)
                on_line(line)
            status = process.wait()
            self._check_cancelled()
            if status:
                detail = next((line.removeprefix("ERROR:").strip() for line in reversed(tail)
                               if line.startswith("ERROR:")), "\n".join(tail))
                raise RuntimeError(detail[-1000:] or "The audio download failed. Please try again.")
        finally:
            if process.poll() is None:
                self._signal(process, signal.SIGKILL)
                process.wait()
            process.stdout.close()
            with self._lock:
                self._process = None

    def _convert(self, arguments):
        self.progress("Preparing session audio…", None)
        self._run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *arguments])

    def run(self) -> dict:
        executable = shutil.which("yt-dlp")
        if not executable:
            raise RuntimeError("yt-dlp is not installed. Install it to download YouTube audio.")
        self._check_cancelled()
        self.progress("Connecting to YouTube…", None)
        with tempfile.TemporaryDirectory(prefix=".youtube-", dir=self.session.folder) as directory:
            folder = Path(directory)
            title, downloaded = "YouTube backing track", None

            def output(line):
                nonlocal title, downloaded
                if line.startswith("RIFF_TITLE "):
                    title = " ".join(json.loads(line[len("RIFF_TITLE "):]).split()) or title
                    self.progress(f"Downloading {title}…", None)
                elif line.startswith("RIFF_FILE "):
                    downloaded = Path(json.loads(line[len("RIFF_FILE "):])).resolve()
                elif line.startswith("RIFF_PROGRESS "):
                    fields = line.split()
                    try:
                        done, total = float(fields[1]), float(fields[2])
                    except (IndexError, ValueError):
                        return
                    fraction = min(1, max(0, done / total)) if total > 0 else None
                    self.progress("Downloading audio…", fraction)

            self._run([
                executable, "--ignore-config", "--no-plugin-dirs", "--no-cache-dir",
                "--no-playlist", "--no-simulate", "--no-warnings", "--newline", "--progress",
                "--progress-delta", "0.25", "--color", "never", "--socket-timeout", "20",
                "--retries", "2", "--fragment-retries", "2", "--extractor-retries", "2",
                "--match-filters", "!is_live & !is_upcoming", "--format", "bestaudio/best",
                "--output", str(folder / "audio.%(ext)s"),
                "--print", "before_dl:RIFF_TITLE %(title)j",
                "--print", "after_move:RIFF_FILE %(filepath)j",
                "--progress-template", "download:RIFF_PROGRESS %(progress.downloaded_bytes|0)s "
                "%(progress.total_bytes,progress.total_bytes_estimate|0)s",
                "--", self.url,
            ], output)
            self._check_cancelled()
            if not downloaded or downloaded.parent != folder.resolve() or not downloaded.is_file():
                raise RuntimeError("No audio was downloaded. Choose a regular video; live or upcoming streams aren't supported.")
            item = import_backing(downloaded, self.session, convert=self._convert)
            try:
                self._check_cancelled()
            except DownloadCancelled:
                self.session.path(item).unlink(missing_ok=True)
                raise
            item.update({"name": title, "source_url": self.url})
            return item
