"""Choose real camera modes instead of enlarging unsupported resolutions."""

import unittest

from riff.webcam import BALANCED, HIGH, Camera, Gst, capture_mode


class CameraModes(unittest.TestCase):
    def test_high_can_use_native_jpeg_while_balanced_uses_native_raw(self):
        camera = Camera("test", "Camera", "video/x-raw,width=1280,height=720,framerate=30/1;"
                                         "image/jpeg,width=1920,height=1080,framerate=30/1")
        for quality, media_type, size in ((BALANCED, "video/x-raw", (1280, 720)),
                                         (HIGH, "image/jpeg", (1920, 1080))):
            with self.subTest(quality=quality.key):
                mode = capture_mode(camera, quality)
                self.assertEqual((mode.width, mode.height), size)
                self.assertTrue(mode.input_caps.startswith(media_type))
                self.assertTrue(Gst.Caps.from_string(camera.caps).can_intersect(Gst.Caps.from_string(mode.input_caps)))

    def test_lower_resolution_and_frame_rate_are_preserved(self):
        camera = Camera("test", "Camera", "video/x-raw,width=640,height=480,framerate=15/1")
        for quality in (BALANCED, HIGH):
            mode = capture_mode(camera, quality)
            self.assertEqual((mode.width, mode.height, mode.fps_num, mode.fps_den), (640, 480, 15, 1))

    def test_720p30_is_preferred_over_1080p15(self):
        camera = Camera("test", "Camera", "video/x-raw,width=1280,height=720,framerate=30/1;"
                                         "image/jpeg,width=1920,height=1080,framerate=15/1")
        mode = capture_mode(camera, HIGH)
        self.assertEqual((mode.width, mode.height, mode.fps_num, mode.fps_den), (1280, 720, 30, 1))

    def test_ranged_caps_are_fixed_to_the_requested_size_and_rate(self):
        camera = Camera("test", "Camera", "video/x-raw,width=(int)[160,1920],height=(int)[120,1080],"
                                         "framerate=(fraction)[1/1,60/1],format=(string){YUY2,NV12}")
        for quality in (BALANCED, HIGH):
            mode = capture_mode(camera, quality)
            self.assertEqual((mode.width, mode.height, mode.fps_num), (quality.width, quality.height, 30))
            self.assertTrue(Gst.Caps.from_string(mode.input_caps).is_fixed())

    def test_larger_native_modes_are_downscaled_and_higher_rates_reduced(self):
        camera = Camera("test", "Camera", "video/x-raw,width=3840,height=2160,framerate=60/1;"
                                         "video/x-raw,width=640,height=480,framerate=15/1")
        mode = capture_mode(camera, HIGH)
        native = Gst.Caps.from_string(mode.input_caps).get_structure(0)
        self.assertEqual(native.get_int("width")[1], 3840)
        self.assertEqual(native.get_fraction("framerate")[1:], (60, 1))
        self.assertEqual((mode.width, mode.height, mode.fps_num), (1920, 1080, 30))

    def test_unsupported_camera_has_an_actionable_error(self):
        with self.assertRaisesRegex(RuntimeError, "no usable video mode"):
            capture_mode(Camera("test", "Camera", "application/octet-stream"), HIGH)


if __name__ == "__main__":
    unittest.main()
