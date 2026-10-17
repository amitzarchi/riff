"""Omarchy palettes and flat GTK styling; no desktop configuration is changed."""

from pathlib import Path
import tomllib

DEFAULTS = {
    "mode": "dark", "background": "#282828", "dark_background": "#1e1e1e",
    "lighter_background": "#3c3836", "foreground": "#d4be98", "dark_foreground": "#928374",
    "accent": "#7daea3", "red": "#ea6962", "yellow": "#d8a657", "green": "#a9b665",
    "muted": "#665c54",
}
LIGHT_DEFAULTS = {
    **DEFAULTS, "mode": "light", "background": "#ffffff", "dark_background": "#f5f5f5",
    "lighter_background": "#e0e0e0", "foreground": "#202020", "dark_foreground": "#6b6b6b",
    "accent": "#3b6860", "red": "#a62e2e", "yellow": "#855b15", "green": "#356c38",
    "muted": "#a0a0a0",
}


def _rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _mix(first, second, amount):
    return "#" + "".join(f"{round((a * (1 - amount) + b * amount) * 255):02x}"
                          for a, b in zip(_rgb(first), _rgb(second)))


def _luminance(value):
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(value)]
    return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(first, second):
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def _readable(color, foreground, backgrounds):
    for amount in range(21):
        candidate = _mix(color, foreground, amount / 20)
        if min(contrast(candidate, background) for background in backgrounds) >= 4.5:
            return candidate
    return foreground


def _tokens(palette):
    c = palette.copy()
    background, surface, foreground = c["background"], c["dark_background"], c["foreground"]
    c["border"] = _mix(background, foreground, 0.24)
    c["hover"] = _mix(background, foreground, 0.09)
    c["muted_text"] = _readable(c["dark_foreground"], foreground, (background, surface))
    c["backing_color"] = _readable(c["accent"], foreground, (background, surface))
    c["guitar_color"] = _readable(c["yellow"], foreground, (background, surface))
    c["metronome_color"] = _readable(c["green"], foreground, (background, surface))
    c["error_color"] = _readable(c["red"], foreground, (background, surface))
    c["on_accent"] = max(("#000000", "#ffffff"), key=lambda text: contrast(text, c["accent"]))
    c["on_record"] = max(("#000000", "#ffffff"), key=lambda text: contrast(text, c["red"]))
    c["selected_take"] = _mix(surface, c["yellow"], 0.12)
    return c


def colors(paths=None, *, fallback=None):
    paths = paths if paths is not None else (
        Path.home() / ".local/state/omarchy/current/theme/colors.toml",
        Path.home() / ".config/omarchy/current/theme/colors.toml",
    )
    for path in paths:
        try:
            data = tomllib.loads(path.read_text())
            valid = {k: v for k, v in data.items() if isinstance(v, str) and
                     v.startswith("#") and len(v) == 7 and
                     all(c in "0123456789abcdefABCDEF" for c in v[1:])}
            if not {"background", "foreground"} <= valid.keys():
                continue
            mode = data.get("mode")
            if mode not in ("light", "dark"):
                mode = "light" if _luminance(valid["background"]) > 0.5 else "dark"
            palette = {**(LIGHT_DEFAULTS if mode == "light" else DEFAULTS), **valid, "mode": mode}
            for key, amount in (("dark_background", 0.035), ("lighter_background", 0.12),
                                ("dark_foreground", 0.55), ("muted", 0.3)):
                if key not in valid:
                    palette[key] = _mix(palette["background"], palette["foreground"], amount)
            return _tokens(palette)
        except (OSError, ValueError):
            continue
    # Omarchy replaces the entire theme directory on a switch. Keep the last
    # valid palette while it is absent/incomplete instead of flashing a fallback.
    return fallback.copy() if fallback is not None else _tokens(DEFAULTS)


def css(c):
    return """
    window.riff { background: %(background)s; color: %(foreground)s; box-shadow: none; }
    .riff, .riff * { border-radius: 0; }
    .riff { font-family: monospace; font-size: 12px; }
    .riff headerbar { background: %(background)s; color: %(foreground)s; box-shadow: none; border-bottom: 1px solid %(border)s; min-height: 32px; padding: 5px 12px; }
    .riff button { color: %(foreground)s; background: transparent; background-image: none; box-shadow: none; border: 1px solid %(border)s; min-height: 24px; padding: 4px 10px; font-weight: 500; }
    .riff button:hover { background: %(hover)s; border-color: %(muted_text)s; }
    .riff button:active, .riff button:checked { background: %(hover)s; border-color: %(accent)s; color: %(backing_color)s; }
    .riff button:disabled { opacity: 0.35; }
    .riff button:focus-visible, .riff dropdown:focus-visible, .riff scale:focus-visible { outline: 1px solid %(accent)s; outline-offset: 2px; }
    .riff button.flat { background: transparent; border-color: transparent; }
    .riff button.image-button { padding-left: 7px; padding-right: 7px; }
    .riff button.flat:hover, .riff button.flat:checked { background: %(hover)s; border-color: %(border)s; }
    .riff button.flat:checked { color: %(backing_color)s; border-color: %(accent)s; }
    .riff button.record { background: %(red)s; color: %(on_record)s; border-color: %(red)s; font-weight: 700; padding: 5px 16px; }
    .riff button.record:hover { background: %(red)s; opacity: 0.88; }
    .riff button.outlined { background: transparent; border-color: %(border)s; }
    .riff button.outlined:hover { background: %(hover)s; border-color: %(muted_text)s; }
    .riff button.export { background: %(accent)s; color: %(on_accent)s; border-color: %(accent)s; font-weight: 700; }
    .riff button.export:hover { background: %(accent)s; opacity: 0.88; }
    .riff .brand { font-size: 18px; font-weight: 700; letter-spacing: -1px; }
    .riff .eyebrow { font-size: 10px; letter-spacing: 1px; color: %(muted_text)s; }
    .riff .muted { color: %(muted_text)s; }
    .riff .small { font-size: 11px; }
    .riff .mono { font-family: monospace; }
    .riff .clock { font-size: 24px; font-weight: 500; letter-spacing: -1px; }
    .riff entry { background: %(dark_background)s; color: %(foreground)s; border: 1px solid %(border)s; box-shadow: none; caret-color: %(foreground)s; min-height: 28px; padding: 4px 8px; }
    .riff entry:focus-within { border-color: %(accent)s; outline: none; }
    .riff entry selection { background: %(accent)s; color: %(on_accent)s; }
    .riff .session-name { background: transparent; border-color: transparent; box-shadow: none; font-size: 16px; font-weight: 600; padding: 0; min-height: 24px; }
    .riff .session-name:focus-within { border-color: transparent; border-bottom-color: %(accent)s; }
    .riff .lane { background: %(dark_background)s; border: 1px solid %(border)s; }
    .riff .lane-controls { border-right: 1px solid %(border)s; padding: 14px; }
    .riff .track-name { font-size: 12px; font-weight: 700; }
    .riff .backing-accent { color: %(backing_color)s; }
    .riff .guitar-accent { color: %(guitar_color)s; }
    .riff .metronome-accent { color: %(metronome_color)s; }
    .riff .transport { background: %(dark_background)s; border: 1px solid %(border)s; padding: 9px 12px; }
    .riff .footer { background: %(dark_background)s; border-top: 1px solid %(border)s; padding: 9px 18px; }
    .riff .webcam-panel { background: %(background)s; }
    .riff .camera-frame { background: %(dark_background)s; border: 1px solid %(border)s; }
    .riff paned > separator { background: %(border)s; min-width: 5px; }
    .riff paned > separator:hover { background: %(accent)s; }
    .riff .take { background: transparent; border: 1px solid %(border)s; padding: 9px 28px 9px 12px; }
    .riff .take.selected { color: %(guitar_color)s; border-color: %(guitar_color)s; background: %(selected_take)s; }
    .riff button.take-delete { color: %(muted_text)s; background: transparent; border: none; min-width: 16px; min-height: 16px; padding: 0; font-size: 12px; }
    .riff button.take-delete:hover { color: %(error_color)s; background: %(hover)s; }
    .riff .error { color: %(error_color)s; }
    .riff .status-dot { color: %(green)s; }
    .riff scale { padding: 6px 0; }
    .riff scale trough { background: %(border)s; border: none; min-height: 2px; }
    .riff scale highlight { background: %(accent)s; border: none; min-height: 2px; min-width: 0; margin: 0; }
    .riff scale slider { background: %(foreground)s; background-image: none; border: none; box-shadow: none; min-width: 5px; min-height: 12px; margin: -5px 0; }
    .riff levelbar trough { background: %(border)s; padding: 0; border: none; min-height: 5px; }
    .riff levelbar block.filled { background: %(green)s; border: none; }
    .riff levelbar block.empty { background: %(border)s; border: none; }
    .riff separator { background: %(border)s; min-height: 1px; }
    .riff progressbar trough { background: %(border)s; min-height: 4px; }
    .riff progressbar progress { background: %(accent)s; border: none; }
    .riff popover contents { background: %(background)s; color: %(foreground)s; border: 1px solid %(border)s; box-shadow: none; }
    .riff dropdown button, .riff spinbutton { background: %(dark_background)s; color: %(foreground)s; border: 1px solid %(border)s; box-shadow: none; }
    .riff spinbutton entry { border: none; }
    .riff spinbutton button { border-width: 0 0 0 1px; }
    .riff listview, .riff list { background: %(background)s; color: %(foreground)s; }
    .riff listview row:hover, .riff list row:hover { background: %(hover)s; }
    .riff listview row:selected, .riff list row:selected { background: %(accent)s; color: %(on_accent)s; }
    .riff scrollbar trough { background: transparent; }
    .riff scrollbar slider { background: %(border)s; min-width: 4px; min-height: 4px; }
    .riff scrollbar.timeline-scroll { background: %(background)s; }
    .riff scrollbar.timeline-scroll trough { background: %(border)s; min-height: 6px; border: none; }
    .riff scrollbar.timeline-scroll slider { background: %(muted_text)s; min-width: 24px; min-height: 6px; margin: 0; }
    .riff tooltip { background: %(background)s; color: %(foreground)s; border: 1px solid %(border)s; box-shadow: none; }
    """ % c
