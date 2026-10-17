from pathlib import Path
import tomllib

DEFAULTS = {
    "background": "#282828", "dark_background": "#1e1e1e", "lighter_background": "#3c3836",
    "foreground": "#d4be98", "dark_foreground": "#928374", "accent": "#7daea3",
    "red": "#ea6962", "yellow": "#d8a657", "green": "#a9b665", "muted": "#665c54",
}


def colors():
    result = DEFAULTS.copy()
    for path in (Path.home() / ".local/state/omarchy/current/theme/colors.toml",
                 Path.home() / ".config/omarchy/current/theme/colors.toml"):
        try:
            data = tomllib.loads(path.read_text())
            result.update({k: v for k, v in data.items() if isinstance(v, str) and
                           v.startswith("#") and len(v) == 7 and
                           all(c in "0123456789abcdefABCDEF" for c in v[1:])})
            break
        except (OSError, ValueError):
            continue
    return result


def css(c):
    return """
    window.riff { background: %(dark_background)s; color: %(foreground)s; }
    .riff { font-family: 'Inter', 'Noto Sans', sans-serif; font-size: 13px; }
    .riff headerbar { background: %(dark_background)s; box-shadow: none; border-bottom: 1px solid %(lighter_background)s; min-height: 48px; }
    .riff button { border-radius: 6px; box-shadow: none; border: 1px solid transparent; min-height: 30px; padding: 4px 12px; }
    .riff button:hover { background: %(lighter_background)s; }
    .riff button.flat { background: transparent; }
    .riff button.image-button { padding-left: 8px; padding-right: 8px; }
    .riff button.flat:hover { background: %(lighter_background)s; }
    .riff button.record { background: %(red)s; color: %(dark_background)s; font-weight: 700; padding: 7px 22px; }
    .riff button.record:disabled { opacity: 0.4; }
    .riff button.outlined { background: transparent; border-color: %(muted)s; }
    .riff button.export { background: %(accent)s; color: %(dark_background)s; font-weight: 700; }
    .riff .brand { font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 800; letter-spacing: -1px; }
    .riff .eyebrow { font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 2px; color: %(dark_foreground)s; }
    .riff .muted { color: %(dark_foreground)s; }
    .riff .small { font-size: 11px; }
    .riff .mono { font-family: 'JetBrains Mono', monospace; }
    .riff .clock { font-family: 'JetBrains Mono', monospace; font-size: 27px; font-weight: 400; letter-spacing: -1px; }
    .riff .session-name { background: transparent; border: none; box-shadow: none; font-size: 19px; font-weight: 600; padding: 0; min-height: 30px; }
    .riff .lane { background: %(background)s; border: 1px solid %(lighter_background)s; border-radius: 8px; }
    .riff .lane-controls { border-right: 1px solid %(lighter_background)s; padding: 20px; }
    .riff .track-name { font-size: 14px; font-weight: 600; }
    .riff .backing-accent { color: %(accent)s; }
    .riff .guitar-accent { color: %(yellow)s; }
    .riff .transport { padding: 14px 0; }
    .riff .footer { border-top: 1px solid %(lighter_background)s; padding: 12px 22px; }
    .riff .take { background: transparent; border: 1px solid %(lighter_background)s; padding: 8px 14px; }
    .riff .take.selected { border-color: %(yellow)s; background: %(background)s; }
    .riff .error { color: %(red)s; }
    .riff .status-dot { color: %(green)s; }
    .riff scale { padding: 8px 0; }
    .riff scale trough { background: %(lighter_background)s; min-height: 3px; }
    .riff scale highlight { background: %(accent)s; min-height: 3px; }
    .riff scale slider { background: %(foreground)s; min-width: 9px; min-height: 9px; margin: -4px; }
    .riff levelbar trough { background: %(lighter_background)s; padding: 0; border: none; min-height: 5px; }
    .riff levelbar block.filled { background: %(green)s; border: none; }
    .riff levelbar block.empty { background: %(lighter_background)s; border: none; }
    .riff separator { background: %(lighter_background)s; }
    .riff entry { background: %(background)s; color: %(foreground)s; border: 1px solid %(muted)s; border-radius: 6px; }
    .riff progressbar trough { background: %(lighter_background)s; border-radius: 3px; min-height: 6px; }
    .riff progressbar progress { background: %(accent)s; border-radius: 3px; }
    .riff popover contents { background: %(background)s; }
    .riff dropdown button { background: %(background)s; border: 1px solid %(lighter_background)s; }
    .riff tooltip { background: %(background)s; color: %(foreground)s; }
    """ % c
