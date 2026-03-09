"""macOS/USB camera source with real motion detection."""
import time
import logging
import numpy as np
import cv2
from .base import CameraSource

logger = logging.getLogger(__name__)


class MacOSSource(CameraSource):
    """macOS / USB camera via OpenCV VideoCapture with real motion detection."""
    
    def __init__(self, width: int, height: int, camera_id: str):
        super().__init__(width, height, camera_id)
        self.cap = None
        
        # Motion detection parameters
        self._prev_frame = None
        self._motion_threshold = 25  # Pixel difference threshold
        self._min_motion_area = 500  # Minimum pixels that must change
        self._detection_cooldown = 3.0  # Stay at 30fps for 3s after motion stops
        self._last_motion_time = 0
        
        # Running average for adaptive threshold
        self._frame_count = 0
        self._motion_history = []
        self._history_size = 30  # Track last 30 frames
    
    def open(self):
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if not self.cap.isOpened():
            raise RuntimeError("Could not open macOS/USB camera")
        logger.info(f"macOS camera opened at {self.width}x{self.height}")
        logger.info(f"Motion detection: threshold={self._motion_threshold}, min_area={self._min_motion_area}")
    
    def close(self):
        if self.cap:
            self.cap.release()
    
    def _detect_motion(self, frame: np.ndarray) -> tuple[bool, int]:
        """Detect motion by comparing with previous frame.
        
        Returns:
            (motion_detected, motion_pixels)
        """
        # Convert to grayscale for motion detection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)
        
        # First frame - initialize
        if self._prev_frame is None:
            self._prev_frame = gray
            return False, 0
        
        # Compute absolute difference
        frame_delta = cv2.absdiff(self._prev_frame, gray)
        thresh = cv2.threshold(frame_delta, self._motion_threshold, 255, cv2.THRESH_BINARY)[1]
        
        # Dilate to fill holes
        thresh = cv2.dilate(thresh, None, iterations=2)
        
        # Count motion pixels
        motion_pixels = np.sum(thresh > 0)
        
        # Update previous frame
        self._prev_frame = gray
        
        # Track motion history
        self._motion_history.append(motion_pixels)
        if len(self._motion_history) > self._history_size:
            self._motion_history.pop(0)
        
        # Detect motion if enough pixels changed
        motion_detected = motion_pixels > self._min_motion_area
        
        return motion_detected, int(motion_pixels)
    
    def grab_frame(self) -> np.ndarray:
        ret, frame = self.cap.read()
        if not ret:
            raise RuntimeError("Frame grab failed")
        
        # Detect motion
        motion_detected, motion_pixels = self._detect_motion(frame)
        
        # Update detection state
        was_detecting = self._detection_active
        
        if motion_detected:
            self._detection_active = True
            self._last_motion_time = time.time()
            if not was_detecting:
                avg_motion = np.mean(self._motion_history[-10:]) if len(self._motion_history) >= 10 else motion_pixels
                logger.info(f"🔴 Motion detected: {motion_pixels} pixels (avg: {avg_motion:.0f})")
        else:
            # Check cooldown
            time_since_motion = time.time() - self._last_motion_time
            if self._detection_active and time_since_motion > self._detection_cooldown:
                self._detection_active = False
                logger.info(f"🟢 Motion ended (cooldown: {time_since_motion:.1f}s)")
        
        # Overlay detection status
        status = "MOTION DETECTED" if self._detection_active else "IDLE"
        color = (0, 0, 255) if self._detection_active else (0, 255, 0)
        cv2.putText(frame, status, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        
        # Show motion pixels count
        cv2.putText(frame, f"Motion: {motion_pixels} px", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        
        self._frame_count += 1
        
        return frame
    
    def get_detection_info(self) -> dict:
        elapsed_since_motion = time.time() - self._last_motion_time if self._last_motion_time > 0 else 0
        avg_motion = np.mean(self._motion_history) if self._motion_history else 0
        
        return {
            "detection_active": self._detection_active,
            "motion_detection": True,
            "motion_pixels": int(self._motion_history[-1]) if self._motion_history else 0,
            "avg_motion_pixels": int(avg_motion),
            "seconds_since_motion": round(elapsed_since_motion, 1),
            "cooldown_remaining": max(0, self._detection_cooldown - elapsed_since_motion) 
                if self._detection_active else 0,
            "frame_count": self._frame_count,
        }
