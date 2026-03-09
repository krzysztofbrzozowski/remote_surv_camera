"""Raspberry Pi AI Camera (IMX500) source with hardware object detection."""
import time
import logging
import numpy as np
from .base import CameraSource

logger = logging.getLogger(__name__)


class RPiAISource(CameraSource):
    """Raspberry Pi AI Camera via picamera2 with IMX500 hardware detection.
    
    The IMX500 sensor has built-in AI acceleration for object detection.
    When objects are detected, streaming switches from 0.1fps to 30fps.
    """
    
    def __init__(self, width: int, height: int, camera_id: str):
        super().__init__(width, height, camera_id)
        self.picam = None
        self._last_detection_time = 0
        self._detection_cooldown = 3.0  # Seconds to stay at 30fps after last detection
        self._detections = []
    
    def open(self):
        try:
            from picamera2 import Picamera2
            from picamera2.devices.imx500 import IMX500
            
            self.picam = Picamera2(IMX500)
            
            # Configure for 2K resolution (1920x1080 or 2048x1536)
            # IMX500 AI Camera supports up to 4056x3040
            # Use 1920x1080 for 2K
            config = self.picam.create_still_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                buffer_count=2
            )
            self.picam.configure(config)
            
            # Enable IMX500 object detection
            # The IMX500 has built-in neural network for detection
            try:
                self.picam.start_imx500_detection()
                logger.info("IMX500 object detection enabled")
            except AttributeError:
                logger.warning("IMX500 detection not available, using standard mode")
            
            self.picam.start()
            logger.info(f"RPi AI Camera opened at {self.width}x{self.height}")
            
        except ImportError as e:
            logger.error(f"Failed to import picamera2 or IMX500: {e}")
            raise RuntimeError("picamera2 with IMX500 support required for RPi AI Camera")
        except Exception as e:
            logger.error(f"Failed to initialize RPi AI Camera: {e}")
            raise
    
    def close(self):
        if self.picam:
            try:
                self.picam.stop()
            except:
                pass
    
    def grab_frame(self) -> np.ndarray:
        frame = self.picam.capture_array()
        
        # Check for detections from IMX500
        self._update_detection_state()
        
        return frame
    
    def _update_detection_state(self):
        """Update detection state from IMX500 hardware detector."""
        try:
            # Try to get detection results from IMX500
            # The exact API depends on picamera2 version
            if hasattr(self.picam, 'get_imx500_detections'):
                detections = self.picam.get_imx500_detections()
            elif hasattr(self.picam, 'metadata'):
                # Check metadata for detection results
                metadata = self.picam.metadata
                detections = metadata.get('detections', []) if metadata else []
            else:
                detections = []
            
            # Update detection state
            if detections and len(detections) > 0:
                self._detections = detections
                was_detecting = self._detection_active
                self._detection_active = True
                self._last_detection_time = time.time()
                
                if not was_detecting:
                    logger.info(f"🔴 Detection started: {len(detections)} objects")
            
            # Check cooldown - keep detecting for a few seconds after last detection
            elif self._detection_active:
                if time.time() - self._last_detection_time > self._detection_cooldown:
                    self._detection_active = False
                    self._detections = []
                    logger.info("🟢 Detection ended (cooldown expired)")
        
        except Exception as e:
            logger.debug(f"Detection check error: {e}")
            pass
    
    def get_detection_info(self) -> dict:
        """Return current detection state and detected objects."""
        info = {
            "detection_active": self._detection_active,
            "detection_count": len(self._detections),
            "cooldown_remaining": max(0, self._detection_cooldown - (time.time() - self._last_detection_time))
                if self._detection_active else 0,
        }
        
        # Add detection details if available
        if self._detections:
            info["detections"] = []
            for det in self._detections[:5]:  # Limit to 5 for metadata size
                if isinstance(det, dict):
                    info["detections"].append({
                        "class": det.get("class", "unknown"),
                        "confidence": det.get("confidence", 0.0),
                        "bbox": det.get("bbox", [])
                    })
        
        return info
