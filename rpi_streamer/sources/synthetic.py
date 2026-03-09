"""Synthetic test pattern camera source."""
import time
import logging
from datetime import datetime
import numpy as np
import cv2
from .base import CameraSource

logger = logging.getLogger(__name__)


class SyntheticSource(CameraSource):
    """Generates a synthetic test pattern — works on any platform."""
    
    def open(self):
        logger.info("Synthetic camera source ready")
    
    def close(self):
        pass
    
    def grab_frame(self) -> np.ndarray:
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        t = time.time()
        
        # Animated gradient background
        for y in range(self.height):
            shift = int(50 * np.sin(t + y / 50))
            frame[y, :, 0] = np.clip((y * 255 // self.height) + shift, 0, 255)
            frame[y, :, 1] = np.clip(128 + shift, 0, 255)
            frame[y, :, 2] = np.clip(255 - (y * 255 // self.height) + shift, 0, 255)
        
        # Animated circle
        cx = int(self.width / 2 + 100 * np.sin(t))
        cy = int(self.height / 2 + 80 * np.cos(t * 0.7))
        cv2.circle(frame, (cx, cy), 30, (0, 255, 0), -1)
        cv2.circle(frame, (cx, cy), 32, (255, 255, 255), 2)
        
        # Timestamp
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(frame, f"[{self.camera_id}] {ts}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        cv2.putText(frame, "SYNTHETIC FEED", (10, self.height - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        
        return frame
