"""Local sessions and lossless audio import/export. No service or database."""

from __future__ import annotations

import io
import json
import os
import subprocess
import wave
from array import array
from datetime import datetime
from pathlib import Path
from uuid import uuid4

RATE = 48_000
PEAK_RATE = 50
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "riff"
SESSION_DIR = Path.home() / "Music" / "Riff"


class _TakeFile(io.BufferedWriter):
    def __init__(self, path):
        super().__init__(open(path, "wb", buffering=0))
        self.silence = False

    def write(self, data):
        if self.silence:
            self.seek(len(data), io.SEEK_CUR)
            return len(data)
        return super().write(data)


def open_take(path, start_frames=0):
    """Create a WAV with sparse leading silence, keeping audio on session time."""
    output = _TakeFile(path)
    writer = None
    try:
        writer = wave.open(output, "wb")
        writer.setparams((2, 3, RATE, 0, "NONE", "not compressed"))
        writer.writeframes(b"")
        if start_frames:
            output.silence = True
            zeros = bytes(RATE * 6)
            while start_frames:
                frames = min(RATE, start_frames)
                writer.writeframesraw(memoryview(zeros)[:frames * 6])
                start_frames -= frames
            output.silence = False
            writer.writeframes(b"")
            output.truncate()
        return writer, output
    except Exception:
        output.silence = False
        try:
            if writer:
                writer.close()
        finally:
            output.close()
        raise


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


class Session:
    def __init__(self, folder: Path, data: dict):
        self.folder = folder
        self.data = data

    @classmethod
    def new(cls, root: Path | None = None):
        now = datetime.now()
        folder = (root or SESSION_DIR) / f"{now:%Y-%m-%d_%H-%M-%S}_{uuid4().hex[:4]}"
        session = cls(folder, {"version": 1, "name": f"Session · {now:%d %b}",
                               "backing": None, "takes": [], "selected": None,
                               "backing_gain": 0.65, "guitar_gain": 1.0, "alignment_ms": 0,
                               "playback_start": 0.0,
                               "metronome_enabled": False, "metronome_bpm": 120, "metronome_gain": 0.3,
                               "metronome_alignment_ms": 0})
        session.save()
        return session

    @classmethod
    def open(cls, folder: Path):
        data = read_json(folder / "session.json")
        if data.get("version") != 1 or not isinstance(data.get("takes"), list):
            raise ValueError("Choose a Riff session folder containing session.json.")
        session = cls(folder, data)
        recovered = False
        # Files are always confined to the session folder.
        for item in [data.get("backing"), *data["takes"]]:
            if not item:
                continue
            for path in session.media_paths(item):
                if not path.is_file():
                    # An interrupted deletion must restore every referenced media file.
                    for archive in folder.glob("deleted-takes-*"):
                        staged = archive / path.name
                        if (not archive.is_symlink() and staged.is_file()
                                and staged.resolve().parent == archive.resolve()):
                            staged.replace(path)
                            try:
                                archive.rmdir()
                            except OSError:
                                pass
                            break
                if not path.is_file():
                    if path == session.path(item):
                        raise ValueError(f"Missing session audio: {item.get('file', '?')}")
                    # A missing video must not prevent access to the lossless audio.
                    item.pop("video", None)
                    item.pop("video_start", None)
                    recovered = True
        # writeframes patches the WAV header on every buffer. A killed process can
        # leave valid audio without its final metadata entry; recover it on open.
        known = {item["file"] for item in data["takes"]}
        for path in sorted(folder.glob("take-*.wav")):
            if path.name in known:
                continue
            try:
                with wave.open(str(path), "rb") as audio:
                    duration = audio.getnframes() / audio.getframerate()
                if duration > 0:
                    ident = path.stem.removeprefix("take-")
                    take = {"id": ident, "file": path.name,
                            "name": f"Recovered take {len(data['takes']) + 1:02}", "duration": duration}
                    session.attach_video(take)
                    data["takes"].append(take)
                    data["selected"] = ident
                    recovered = True
            except (OSError, EOFError, wave.Error):
                continue
        if recovered:
            session.save()
        return session

    def path(self, item: dict) -> Path:
        path = (self.folder / item["file"]).resolve()
        if path.parent != self.folder.resolve():
            raise ValueError("Audio must be inside the session folder.")
        return path

    def video_path(self, take):
        return self.path({"file": take["video"]}) if take and take.get("video") else None

    def media_paths(self, take):
        paths = [self.path(take)]
        if video := self.video_path(take):
            paths.append(video)
        partial = self.path({"file": paths[0].with_suffix(".webm.part").name})
        if partial.is_file():
            paths.append(partial)
        return paths

    def attach_video(self, take):
        video = self.path({"file": self.path(take).with_suffix(".webm").name})
        if video.is_file():
            take["video"] = video.name
            take.setdefault("video_start", self.data.get("playback_start", 0.0))

    @property
    def selected(self):
        return next((t for t in self.data["takes"] if t["id"] == self.data.get("selected")), None)

    def save(self):
        atomic_json(self.folder / "session.json", self.data)

    def next_take(self):
        ident = uuid4().hex[:10]
        number = len(self.data["takes"]) + 1
        names = {take["name"] for take in self.data["takes"]}
        while f"Take {number:02}" in names:
            number += 1
        return {"id": ident, "file": f"take-{ident}.wav",
                "name": f"Take {number:02}", "duration": 0.0}

    def remove_takes(self, identifiers=None):
        """Stage deleted audio for Trash, rolling back if moving or saving fails."""
        takes = self.data["takes"]
        identifiers = set(identifiers) if identifiers is not None else {take["id"] for take in takes}
        removed = [take for take in takes if take["id"] in identifiers]
        if not removed:
            return None
        remaining = [take for take in takes if take["id"] not in identifiers]
        protected = {path for take in remaining for path in self.media_paths(take)}
        if backing := self.data.get("backing"):
            protected.add(self.path(backing))
        paths = list(dict.fromkeys(path for take in removed for path in self.media_paths(take)))
        if set(paths) & protected:
            raise ValueError("Cannot delete audio used by the backing track or another take.")
        selected = self.data.get("selected")
        replacement = selected if remaining else None
        if selected in identifiers:
            index = next((i for i, take in enumerate(takes) if take["id"] == selected), 0)
            choices = [take for take in takes[index + 1:] if take["id"] not in identifiers]
            replacement = (choices[0] if choices else remaining[-1])["id"] if remaining else None
        archive = self.folder / f"deleted-takes-{uuid4().hex[:10]}"
        archive.mkdir(mode=0o700)
        moved = []
        try:
            for path in paths:
                if path.exists():
                    staged = archive / path.name
                    path.replace(staged)
                    moved.append((path, staged))
            self.data.update(takes=remaining, selected=replacement)
            self.save()
        except Exception:
            self.data.update(takes=takes, selected=selected)
            for path, staged in reversed(moved):
                staged.replace(path)
            archive.rmdir()
            raise
        return archive

    def commit_take(self, take: dict):
        with wave.open(str(self.path(take)), "rb") as audio:
            take["duration"] = audio.getnframes() / audio.getframerate()
        if take["duration"] <= 0:
            self.path(take).unlink(missing_ok=True)
            return False
        self.attach_video(take)
        self.data["takes"].append(take)
        self.data["selected"] = take["id"]
        self.save()
        return True


def run_ffmpeg(args: list[str]):
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[-1200:] or "Audio conversion failed.")


def import_backing(source: Path, session: Session, *, convert=run_ffmpeg) -> dict:
    name = f"backing-{uuid4().hex[:10]}.wav"
    destination = session.folder / name
    try:
        convert(["-i", str(source), "-vn", "-ar", str(RATE), "-ac", "2",
                 "-c:a", "pcm_s24le", "-rf64", "never", "-n", str(destination)])
        with wave.open(str(destination), "rb") as audio:
            duration = audio.getnframes() / RATE
        if duration <= 0:
            raise ValueError("This file has no audio.")
        return {"file": name, "name": source.stem, "duration": duration}
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def read_peaks(path: Path) -> list[float]:
    """50 real amplitude envelopes per second, bounded by audio duration."""
    peaks = []
    with wave.open(str(path), "rb") as audio:
        width = audio.getsampwidth()
        while raw := audio.readframes(RATE // PEAK_RATE):
            if width == 3:
                # Most significant 16 bits of each signed 24-bit sample.
                pcm = bytearray(len(raw) // 3 * 2)
                pcm[0::2], pcm[1::2] = raw[1::3], raw[2::3]
                raw = pcm
            elif width != 2:
                raise ValueError("Expected 16- or 24-bit WAV audio.")
            samples = array("h", raw)
            if os.sys.byteorder != "little":
                samples.byteswap()
            peaks.append(max(abs(min(samples, default=0)), abs(max(samples, default=0))) / 32768)
        return peaks


def export_audio(session: Session, destination: Path, guitar_only=False):
    """Render to a temporary file, then atomically replace the chosen destination."""
    take, backing = session.selected, session.data.get("backing")
    if not take and (guitar_only or not backing):
        raise ValueError("Record a take first.")
    inputs, filters, labels = [], [], []
    sources = []
    if backing and not guitar_only:
        sources.append((backing, session.data.get("backing_gain", 0.65), 0))
    if take:
        sources.append((take, session.data.get("guitar_gain", 1), session.data.get("alignment_ms", 0)))
    for index, (item, gain, offset) in enumerate(sources):
        inputs.extend(["-i", str(session.path(item))])
        correction = ""
        if offset > 0:
            correction = f"adelay={offset}:all=1,"
        elif offset < 0:
            correction = f"atrim=start={-offset / 1000},asetpts=PTS-STARTPTS,"
        label = f"a{index}"
        filters.append(f"[{index}:a]{correction}volume={gain}[{label}]")
        labels.append(f"[{label}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                   "alimiter=limit=0.98:level=false:latency=true[out]")
    temporary = destination.parent / f".riff-export-{uuid4().hex}.wav"
    try:
        run_ffmpeg([*inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                    "-ar", str(RATE), "-ac", "2", "-c:a", "pcm_s24le", "-rf64", "auto",
                    "-n", str(temporary)])
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
