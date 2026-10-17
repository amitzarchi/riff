#!/usr/bin/env bash
set -euo pipefail
riff_root="$(cd -- "$(dirname -- "$0")" && pwd)"
riff_bin="${HOME}/.local/bin"
riff_apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
riff_icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
mkdir -p "$riff_bin" "$riff_apps" "$riff_icons"
chmod +x "$riff_root/run"
ln -sfn "$riff_root/run" "$riff_bin/riff"
install -m 644 "$riff_root/assets/dev.riff.Recorder.svg" "$riff_icons/dev.riff.Recorder.svg"
cat > "$riff_apps/dev.riff.Recorder.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Riff
GenericName=Guitar Recorder
Comment=Record guitar alongside a backing track
Exec="$riff_root/run"
Icon=dev.riff.Recorder
Terminal=false
Categories=AudioVideo;Audio;Recorder;
Keywords=guitar;record;backing;quad;cortex;
StartupNotify=true
StartupWMClass=dev.riff.Recorder
EOF
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$riff_apps"
fi
printf 'Installed. Launch Riff from the app launcher or run: riff\n'
