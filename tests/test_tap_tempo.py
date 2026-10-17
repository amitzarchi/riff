"""Deterministic rhythm input; no sleeping or physical audio devices."""

import unittest

from riff.metronome import TapTempo


class TapTempoTest(unittest.TestCase):
    def test_two_taps_estimate_tempo_and_more_taps_smooth_timing(self):
        tempo = TapTempo()
        self.assertIsNone(tempo.tap(10))
        self.assertEqual(tempo.tap(10.5), 120)
        self.assertEqual(tempo.tap(11.03), 117)
        self.assertEqual(tempo.tap(11.49), 121)
        self.assertEqual(tempo.tap(12), 120)

    def test_recent_beats_follow_a_new_tempo(self):
        tempo = TapTempo()
        for timestamp in (0, 1, 2, 3, 4):
            bpm = tempo.tap(timestamp)
        self.assertEqual(bpm, 60)
        for timestamp in (4.5, 5, 5.5, 6):
            bpm = tempo.tap(timestamp)
        self.assertEqual(bpm, 120)

    def test_pause_and_explicit_reset_start_a_new_rhythm(self):
        tempo = TapTempo()
        tempo.tap(0)
        self.assertEqual(tempo.tap(0.5), 120)
        self.assertIsNone(tempo.tap(3.5))
        self.assertEqual(tempo.tap(4.5), 60)
        tempo.reset()
        self.assertIsNone(tempo.tap(5.5))
        self.assertEqual(tempo.tap(5.75), 240)

    def test_double_clicks_are_ignored_and_tempo_stays_in_range(self):
        tempo = TapTempo()
        tempo.tap(0)
        self.assertIsNone(tempo.tap(0.04))
        self.assertEqual(tempo.tap(0.5), 120)
        tempo.reset()
        tempo.tap(10)
        self.assertEqual(tempo.tap(10.15), 300)
        tempo.reset()
        tempo.tap(20)
        self.assertEqual(tempo.tap(22), 40)


if __name__ == "__main__":
    unittest.main()
