"""Zoom, pan, and transport coordinates must share the same time viewport."""

import unittest

from riff.timeline import Timeline


class TimelineTest(unittest.TestCase):
    def test_zoom_preserves_the_time_under_the_anchor(self):
        view = Timeline(60)
        view.zoom(2, anchor=15)
        self.assertEqual((view.start, view.end), (7.5, 37.5))
        self.assertEqual(view.fraction_at(15), 0.25)
        view.zoom(2, anchor=15)
        self.assertEqual((view.start, view.end), (11.25, 26.25))
        self.assertEqual(view.time_at(0.25), 15)

    def test_panned_click_coordinates_and_fit(self):
        view = Timeline(120)
        view.zoom(4)
        view.pan_to(75)
        self.assertEqual(view.time_at(0), 75)
        self.assertEqual(view.time_at(0.5), 90)
        self.assertEqual(view.time_at(1), 105)
        self.assertEqual(view.time_at(-1), 75)
        self.assertEqual(view.time_at(2), 105)
        view.fit()
        self.assertEqual((view.start, view.span), (0, 120))
        self.assertTrue(view.fitted)

    def test_zoom_and_scroll_stay_within_the_session(self):
        view = Timeline(60)
        view.zoom(1000, anchor=0)
        self.assertEqual((view.start, view.span), (0, 1))
        view.pan_to(1000)
        self.assertEqual((view.start, view.end), (59, 60))
        view.zoom(0.5, anchor=60)
        self.assertEqual((view.start, view.end), (58, 60))
        view.pan_to(-100)
        self.assertEqual(view.start, 0)
        view.zoom(0.0001)
        self.assertTrue(view.fitted)
        self.assertEqual(view.start, 0)

    def test_recording_growth_preserves_zoom_or_fit(self):
        view = Timeline(10)
        view.set_duration(20)
        self.assertEqual((view.start, view.span), (0, 20))
        view.zoom(4, anchor=0)
        view.pan_to(12)
        view.set_duration(30)
        self.assertEqual((view.start, view.span), (12, 5))
        view.set_duration(10)
        self.assertEqual((view.start, view.span), (5, 5))
        view.set_duration(3)
        self.assertEqual((view.start, view.span), (0, 3))

    def test_follow_reveals_playhead_and_rewind_without_changing_zoom(self):
        view = Timeline(60)
        view.zoom(6, anchor=0)
        view.reveal(12)
        self.assertTrue(view.contains(12))
        self.assertEqual(view.span, 10)
        start = view.start
        view.reveal(15)
        self.assertEqual(view.start, start)
        view.reveal(0)
        self.assertEqual(view.start, 0)
        self.assertEqual(view.span, 10)


if __name__ == "__main__":
    unittest.main()
