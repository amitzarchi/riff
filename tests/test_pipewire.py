"""Opt-in real-server regression: numbered AUX inputs must not become stereo."""

import os
import subprocess
import tempfile
import time
import unittest
import wave
from array import array
from pathlib import Path
from uuid import uuid4

from gi.repository import GLib

from riff.audio import Engine, devices
from riff.storage import RATE, run_ffmpeg


@unittest.skipUnless(os.environ.get("RIFF_TEST_PIPEWIRE") == "1", "opt-in PipeWire loopback test")
class PipeWireChannels(unittest.TestCase):
    def test_aux_inputs_keep_usb_channel_order(self):
        name = "riff_test_" + uuid4().hex[:8]
        channel_map = ",".join(f"aux{i}" for i in range(8))
        modules, errors = [], []
        player = None
        engine = Engine(errors.append)
        try:
            for args in [
                ["module-null-sink", f"sink_name={name}", "channels=8", f"channel_map={channel_map}"],
                ["module-remap-source", f"source_name={name}_input", f"master={name}.monitor",
                 "channels=8", f"channel_map={channel_map}", f"master_channel_map={channel_map}", "remix=no"],
            ]:
                modules.append(subprocess.check_output(["pactl", "load-module", *args], text=True).strip())
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                frequencies = [220, 330, 880, 1100, 440, 550, 660, 770]
                signal = "|".join(f"0.1*sin(2*PI*{freq}*t)" for freq in frequencies)
                run_ffmpeg(["-f", "lavfi", "-i", f"aevalsrc={signal}:s={RATE}:d=3",
                            "-c:a", "pcm_s24le", str(root / "signal.wav")])
                source = next(d for d in devices()[0] if d.name == name + "_input")
                self.assertEqual(source.backend, "pipewire")
                engine.capture(source, (2, 3), root / "take.wav")
                player = subprocess.Popen(["paplay", "--latency-msec=40", "--volume=65536",
                                           f"--device={name}", f"--channel-map={channel_map}",
                                           str(root / "signal.wav")])
                context = GLib.MainContext.default()
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and not errors:
                    while context.pending():
                        context.iteration(False)
                    time.sleep(0.005)
                engine.stop()
                self.assertEqual(errors, [])
                with wave.open(str(root / "take.wav")) as audio:
                    raw = audio.readframes(audio.getnframes())
                pcm = bytearray(len(raw) // 3 * 2)
                pcm[0::2], pcm[1::2] = raw[1::3], raw[2::3]
                samples = array("h", pcm)
                for channel, expected in enumerate((880, 1100)):
                    values = samples[channel::2]
                    active = [i for i, value in enumerate(values) if abs(value) > 30]
                    self.assertTrue(active, f"USB input {channel + 3} is silent")
                    values = values[active[0]:active[-1] + 1]
                    crossings = sum(a <= 0 < b for a, b in zip(values, values[1:]))
                    measured = crossings / (len(values) / RATE)
                    self.assertAlmostEqual(measured, expected, delta=3)
        finally:
            engine.stop()
            if player and player.poll() is None:
                player.terminate()
                player.wait(timeout=3)
            for module in reversed(modules):
                subprocess.run(["pactl", "unload-module", module], check=True)
