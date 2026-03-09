#!/usr/bin/env python3
"""Tests for FPS switching logic with real motion detection."""
import time
import unittest
from unittest.mock import MagicMock, patch
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sources import SyntheticSource, MacOSSource
from sources.base import CameraSource


class TestFPSSwitching(unittest.TestCase):
    """Test dynamic FPS switching based on detection state."""
    
    def test_camera_source_default_fps(self):
        """Camera should default to 1 FPS when no detection."""
        source = SyntheticSource(640, 480, "test-cam")
        self.assertEqual(source.get_target_fps(), 1)
        self.assertFalse(source.has_detection())
    
    def test_camera_source_detection_fps(self):
        """Camera should switch to active FPS (default 30) when detection active."""
        source = SyntheticSource(640, 480, "test-cam")
        source._detection_active = True
        self.assertEqual(source.get_target_fps(), 30)
        self.assertTrue(source.has_detection())
    
    def test_camera_source_custom_active_fps(self):
        """Camera should use configured active FPS."""
        source = SyntheticSource(640, 480, "test-cam")
        source._active_fps = 15
        source._detection_active = True
        self.assertEqual(source.get_target_fps(), 15)


class TestMacOSMotionDetection(unittest.TestCase):
    """Test macOS real motion detection."""
    
    @patch('cv2.VideoCapture')
    def test_motion_detection_initializes(self, mock_cap):
        """Motion detection should initialize properly."""
        import numpy as np
        mock_cap_instance = MagicMock()
        mock_cap_instance.isOpened.return_value = True
        mock_cap_instance.read.return_value = (True, np.zeros((1080, 1920, 3), dtype=np.uint8))
        mock_cap.return_value = mock_cap_instance
        
        source = MacOSSource(1920, 1080, "test-cam")
        source.open()
        
        # Should start in idle state
        self.assertFalse(source.has_detection())
        self.assertEqual(source.get_target_fps(), 1)
        self.assertIsNone(source._prev_frame)
        
        source.close()
    
    @patch('cv2.VideoCapture')
    def test_motion_detection_first_frame(self, mock_cap):
        """First frame should not trigger detection (no reference)."""
        import numpy as np
        mock_cap_instance = MagicMock()
        mock_cap_instance.isOpened.return_value = True
        mock_cap_instance.read.return_value = (True, np.zeros((1080, 1920, 3), dtype=np.uint8))
        mock_cap.return_value = mock_cap_instance
        
        source = MacOSSource(1920, 1080, "test-cam")
        source.open()
        
        # Grab first frame
        frame = source.grab_frame()
        
        # Should not detect motion on first frame
        self.assertFalse(source.has_detection())
        self.assertIsNotNone(source._prev_frame)
        
        source.close()
    
    @patch('cv2.VideoCapture')
    def test_motion_parameters_configurable(self, mock_cap):
        """Motion detection parameters should be configurable."""
        import numpy as np
        mock_cap_instance = MagicMock()
        mock_cap_instance.isOpened.return_value = True
        mock_cap_instance.read.return_value = (True, np.zeros((1080, 1920, 3), dtype=np.uint8))
        mock_cap.return_value = mock_cap_instance
        
        source = MacOSSource(1920, 1080, "test-cam")
        
        # Set custom parameters
        source._motion_threshold = 50
        source._min_motion_area = 1000
        source._detection_cooldown = 5.0
        
        self.assertEqual(source._motion_threshold, 50)
        self.assertEqual(source._min_motion_area, 1000)
        self.assertEqual(source._detection_cooldown, 5.0)
        
        source.open()
        source.close()
    
    @patch('cv2.VideoCapture')
    def test_detection_info_includes_motion_data(self, mock_cap):
        """Detection info should include motion detection data."""
        import numpy as np
        mock_cap_instance = MagicMock()
        mock_cap_instance.isOpened.return_value = True
        mock_cap_instance.read.return_value = (True, np.zeros((1080, 1920, 3), dtype=np.uint8))
        mock_cap.return_value = mock_cap_instance
        
        source = MacOSSource(1920, 1080, "test-cam")
        source.open()
        
        info = source.get_detection_info()
        
        self.assertIn("detection_active", info)
        self.assertIn("motion_detection", info)
        self.assertIn("motion_pixels", info)
        self.assertIn("avg_motion_pixels", info)
        self.assertIn("seconds_since_motion", info)
        self.assertTrue(info["motion_detection"])
        
        source.close()


if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
