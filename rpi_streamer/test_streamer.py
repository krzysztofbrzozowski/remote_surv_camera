"""Tests for main.py — voltage reading and timestamp burn-in."""
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from main import MetadataCollector, PiJuiceCollector, Streamer
from sources import SyntheticSource


class TestVoltageReading(unittest.TestCase):
    """Verify get_voltage() returns PiJuice battery voltage (4-6V range)."""

    def test_voltage_uses_pijuice_battery_voltage(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"battery_voltage_mv": 4200}
        self.assertAlmostEqual(mc.get_voltage(), 4.2, places=2)

    def test_voltage_uses_pijuice_io_voltage_fallback(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"io_voltage_mv": 5100}
        self.assertAlmostEqual(mc.get_voltage(), 5.1, places=2)

    def test_voltage_prefers_battery_over_io(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"battery_voltage_mv": 4500, "io_voltage_mv": 5000}
        self.assertAlmostEqual(mc.get_voltage(), 4.5, places=2)

    def test_voltage_in_expected_range_with_pijuice(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"battery_voltage_mv": 4800}
        v = mc.get_voltage()
        self.assertGreaterEqual(v, 4.0)
        self.assertLessEqual(v, 6.0)

    def test_voltage_falls_back_when_pijuice_unavailable(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {}
        v = mc.get_voltage()
        self.assertGreater(v, 0.0)
        self.assertLess(v, 10.0)

    def test_voltage_not_core_voltage(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"battery_voltage_mv": 4200}
        self.assertGreater(mc.get_voltage(), 1.0)


class TestTimestampBurnIn(unittest.TestCase):
    """Verify timestamp is burned into JPEG frames."""

    def _make_streamer(self):
        config = {
            "server_url": "ws://localhost:8000/ws/camera/feed/",
            "camera_token": "test",
            "camera_id": "test-cam",
            "camera_source": "synthetic",
            "frame_width": 640,
            "frame_height": 480,
            "fps": 15,
            "jpeg_quality": 70,
        }
        return Streamer(config)

    def test_burn_timestamp_modifies_frame(self):
        s = self._make_streamer()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        original_sum = frame.sum()
        result = s._burn_timestamp(frame)
        self.assertGreater(result.sum(), original_sum, "Timestamp should add pixels to frame")

    def test_encode_jpeg_produces_valid_jpeg(self):
        s = self._make_streamer()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        jpeg_data = s.encode_jpeg(frame)
        self.assertTrue(jpeg_data[:2] == b'\xff\xd8', "Should be valid JPEG (starts with FFD8)")
        self.assertGreater(len(jpeg_data), 100)

    def test_burn_timestamp_preserves_dimensions(self):
        s = self._make_streamer()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = s._burn_timestamp(frame)
        self.assertEqual(result.shape, (480, 640, 3))


class TestMetadataVoltageIntegration(unittest.TestCase):
    def test_metadata_voltage_from_pijuice(self):
        mc = MetadataCollector("test-cam")
        mc._pijuice = MagicMock(spec=PiJuiceCollector)
        mc._pijuice.get_data.return_value = {"battery_voltage_mv": 5200}
        meta = mc.get_metadata()
        self.assertAlmostEqual(meta["voltage_v"], 5.2, places=2)
        self.assertGreaterEqual(meta["voltage_v"], 4.0)
        self.assertLessEqual(meta["voltage_v"], 6.0)


if __name__ == "__main__":
    unittest.main()
