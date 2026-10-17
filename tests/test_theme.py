"""Palette reloads must tolerate atomic switches and keep light themes readable."""

import tempfile
import unittest
from pathlib import Path

from riff import theme


class ThemePalette(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "colors.toml"

    def tearDown(self):
        self.temp.cleanup()

    def test_light_palette_has_readable_text_and_button_foregrounds(self):
        self.path.write_text('mode="light"\nbackground="#ffffff"\nforeground="#111111"\n'
                             'dark_background="#f5f5f5"\ndark_foreground="#c0c0c0"\n'
                             'accent="#7a7a7a"\nred="#222222"\nyellow="#dddddd"\n')
        palette = theme.colors([self.path])
        self.assertEqual(palette["mode"], "light")
        for key in ("muted_text", "backing_color", "guitar_color", "metronome_color", "error_color"):
            for background in ("background", "dark_background"):
                self.assertGreaterEqual(theme.contrast(palette[key], palette[background]), 4.5)
        self.assertGreaterEqual(theme.contrast(palette["on_accent"], palette["accent"]), 4.5)
        self.assertGreaterEqual(theme.contrast(palette["on_record"], palette["red"]), 4.5)

    def test_reload_survives_missing_or_incomplete_theme_during_switch(self):
        previous = theme.colors([])
        for content in ('mode="light"\nbackground=', 'accent="#123456"\n', ''):
            self.path.write_text(content)
            self.assertEqual(theme.colors([self.path], fallback=previous), previous)
        self.path.unlink()
        self.assertEqual(theme.colors([self.path], fallback=previous), previous)
        new_path = self.path.with_suffix(".next")
        new_path.write_text('mode="light"\nbackground="#ffffff"\nforeground="#111111"\n')
        new_path.replace(self.path)
        reloaded = theme.colors([self.path], fallback=previous)
        self.assertEqual(reloaded["background"], "#ffffff")
        self.assertNotEqual(reloaded, previous)

    def test_sparse_palette_derives_surfaces_from_its_own_colors(self):
        self.path.write_text('background="#ffffff"\nforeground="#000000"\naccent="#123456"\n')
        palette = theme.colors([self.path])
        self.assertEqual(palette["mode"], "light")
        self.assertGreater(int(palette["dark_background"][1:3], 16), 230)
        self.assertEqual(palette["accent"], "#123456")

    def test_invalid_color_cannot_override_valid_fallback(self):
        self.path.write_text('background="#181818"\nforeground="#eeeeee"\naccent="not-a-color"\n')
        palette = theme.colors([self.path])
        self.assertEqual(palette["mode"], "dark")
        self.assertEqual(palette["accent"], theme.DEFAULTS["accent"])
