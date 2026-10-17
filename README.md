# Riff

A small native recorder for guitar and backing tracks, built for Omarchy. GTK 4.10+,
GStreamer, and FFmpeg; no web server, Electron runtime, or account. YouTube imports
use the installed `yt-dlp`; recording and local sessions work offline.
It reads the current Omarchy palette when it starts (Gruvbox fallback).

## Launch

```sh
./install.sh
riff
```

Or run `./run` directly. Installation adds a **Riff** desktop launcher and
`~/.local/bin/riff`; the source stays in this directory.

Runtime packages on Arch: `python`, `python-gobject`, `python-cairo`, `gtk4`,
`gstreamer`, `gst-plugin-pipewire`, `gst-plugins-base`, `gst-plugins-good`, `gst-plugins-bad`, `gst-libav`,
`ffmpeg`, `libpulse`, and `alsa-lib`. These are already present on this machine.
The Python application uses system packages, with no pip environment or build step.
YouTube import additionally needs `yt-dlp` and a JavaScript runtime supported by
yt-dlp (Deno is installed on this machine).

## Record a guitar part

1. Connect the Quad Cortex over USB. Listen through its headphones or outputs.
2. Open Riff. In **Audio settings**, select **Quad Cortex** and **USB 3 + 4**.
   These normally carry the processed stereo signal; choose **1 + 2** if you have
   enabled the Quad Cortex's USB Dry/Wet Swap. Mono input choices are also available.
3. Click **Add track** (or **Ctrl+O**), paste a YouTube video link, and choose
   **Download & add**. Or choose a local audio file / drop it onto the backing lane
   (WAV, MP3, FLAC, Ogg, M4A, and other FFmpeg formats).
   A backing track is optional; a track playing in another app can accompany guitar-only recording.
4. Press **R** to record. The backing starts at the beginning, and a new stereo take
   is saved at **48 kHz / 24-bit**. Press **R** or **Esc** to finish.
5. Select a take, press **Space** to hear it with the backing, and adjust the two
   volume sliders. Export the mix or guitar only as WAV.

The live input meter does not send guitar audio back to the speakers. The Quad
Cortex provides direct monitoring. Playback uses only output channels 1/2, including
when the device exposes eight USB channels; channels 3–8 receive silence.

If PipeWire does not expose a Quad Cortex input, Riff offers **Quad Cortex · direct
USB** using ALSA capture. This was verified on the connected device. It does not
change the system default device, WirePlumber rules, or sound profiles. Another
application using the same direct capture endpoint may need to release it first.
If the device is unplugged, Riff stops and preserves the current take. Reconnect,
refresh devices in Audio settings, and apply. It never switches to a microphone
while recording.

Numbered Pro Audio / AUX inputs use native PipeWire capture with an explicit
unpositioned channel layout. This avoids the GStreamer PulseAudio source's
`Failed to create stream: Invalid argument` error for those devices and preserves
the numbered USB channel order. Normal microphone inputs use PulseAudio capture.

## Small by design

- One backing track and one selected guitar take; every new recording keeps previous takes.
- Play/pause, stop, seek, rewind, and playback repeat. Recording always starts at zero.
- Two playback/export volume sliders. The recorded guitar WAV remains unaltered.
- A live input meter with a clipping indicator and real waveform envelopes.
- Session name, new/open session, and a button to show the session's files.
- Optional guitar alignment from −500 to +500 ms, applied to playback and export.
  Negative values move a late take earlier. Hardware monitoring avoids a software
  monitoring delay; precise overdub alignment still depends on the device/backend.
- Export sums the selected tracks at their slider levels and peak limits the result
  to prevent mix overload. Raw takes are never normalized or limited.

No effects, editing tools, plugins, tempo grid, or multitrack arrangement. Playback
repeat does not loop recording. A single take ends automatically at one hour so
standard WAV files stay comfortably below their size limit.

## YouTube backing tracks

The add-track dialog shows download progress and supports cancellation, including
during conversion. Riff requests the best available audio stream for one video;
it strips playlist/tracking parameters and does not download playlists or live
streams. Short links, mobile/music links, Shorts, and archived live-video links
are accepted.

The downloaded audio is converted to the same 48 kHz / 24-bit stereo WAV format
as local imports and stored inside the session folder. The track keeps its YouTube
title and source URL; hover over its name to see the link. The converted file is
available offline when reopening the session. Temporary downloads are removed on
success, cancellation, or failure. A replacement only becomes active after the
download and conversion succeed, so previous session audio remains available.

Riff runs yt-dlp without loading personal configuration or plugins. Private,
restricted, unavailable, or network-blocked videos show a download error in the
dialog. You can retry or choose a local file. YouTube changes can require an
updated yt-dlp package.

## Files and shortcuts

Sessions live in `~/Music/Riff/<timestamp>/`, containing `session.json`, a converted
backing WAV, and separate take WAVs. Imported originals are untouched. Session
metadata is saved atomically; audio is written throughout capture and finished on
Stop, window close, SIGINT, or SIGTERM. Valid take WAVs left without a metadata
entry after an interrupted process are recovered when that session is reopened.
The most recent session and device choices are remembered in
`~/.local/state/riff/settings.json` (or `$XDG_STATE_HOME/riff`).

| Key | Action |
| --- | --- |
| Space | Play / pause |
| R | Record / finish take |
| Esc | Stop |
| Home | Back to start |
| Ctrl+O | Add YouTube link or audio file |
| Ctrl+N | New session |
| Ctrl+E | Export mix |

Shortcuts leave text entry alone while you edit a session name.

## Verification

```sh
python -W ignore::DeprecationWarning -m unittest discover -s tests -v
ruff check .
```

The audio integration tests generate known signals and verify stereo channel
selection, simultaneous recording/backing playback, transport controls, WAV
format/duration, positive and negative alignment in playback and export, and
interrupted-session recovery. They use fake audio sinks and never open a microphone.

An opt-in regression checks actual PipeWire channel negotiation using temporary
virtual devices and distinct signals on eight AUX channels. It removes those
devices afterward and does not use physical audio inputs or outputs:

```sh
RIFF_TEST_PIPEWIRE=1 python -W ignore::DeprecationWarning -m unittest discover -s tests -v
```

The native GTK window, direct Quad Cortex USB capture, and silent playback through
the eight-channel Quad Cortex output were also exercised on this machine.

YouTube import tests use a controlled downloader process plus real FFmpeg
conversion to verify URL normalization, progress, retained title/source metadata,
cancellation, temporary-file cleanup, and preservation of the current backing on
failure. They do not need a network connection.
The complete importer was also verified against a live, short YouTube video,
including title extraction, download progress, and the final session WAV format.

USB mapping reference: [Neural DSP — using Quad Cortex as an audio interface](https://neuraldsp.com/getting-started/using-quad-cortex-as-an-audio-interface).

To remove the launcher, delete `~/.local/bin/riff`,
`~/.local/share/applications/dev.riff.Recorder.desktop`, and
`~/.local/share/icons/hicolor/scalable/apps/dev.riff.Recorder.svg`.
Your sessions are independent of the application directory.
