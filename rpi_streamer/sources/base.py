"""Base class for camera sources."""
from abc import ABC, abstractmethod
from typing import Tuple
import numpy as np


class CameraSource(ABC):
    """Abstract base class for all camera sources."""
    
    def __init__(self, width: int, height: int, camera_id: str):
        self.width = width
        self.height = height
        self.camera_id = camera_id
        self._detection_active = False
        self._target_fps = 1      # Idle FPS (default 1)
        self._active_fps = 30     # Active FPS when detection triggered
    
    @abstractmethod
    def open(self):
        """Initialize and open the camera source."""
        pass
    
    @abstractmethod
    def close(self):
        """Close and cleanup the camera source."""
        pass
    
    @abstractmethod
    def grab_frame(self) -> np.ndarray:
        """Capture and return a single frame as numpy array (H, W, 3) RGB."""
        pass
    
    @property
    def encoding_type(self) -> str:
        """Return encoding type: 'jpeg' or 'h264'."""
        return "jpeg"
    
    def has_detection(self) -> bool:
        """Check if detection is active. Override in detection-capable sources."""
        return self._detection_active
    
    def get_target_fps(self) -> float:
        """Get current target FPS based on detection state."""
        return self._active_fps if self._detection_active else self._target_fps
    
    def get_detection_info(self) -> dict:
        """Return detection metadata. Override in detection-capable sources."""
        return {"detection_active": self._detection_active}
    
    def get_encoded_chunks(self) -> list:
        """Get pending H.264 encoded chunks. Override in H.264-capable sources."""
        return []
    
    def check_motion(self):
        """Check for motion on lores stream. Override in H.264 sources."""
        pass
    
    def set_fps(self, fps: float):
        """Set camera framerate dynamically. Override in H.264 sources."""
        pass
