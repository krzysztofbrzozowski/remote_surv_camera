"""Camera source implementations."""
from .base import CameraSource
from .synthetic import SyntheticSource
from .macos import MacOSSource
from .rpi_ai import RPiAISource
from .rpi_regular import RPiRegularSource
from .macos_h264 import MacOSH264Source

__all__ = ["CameraSource", "SyntheticSource", "MacOSSource", "RPiAISource", "RPiRegularSource", "MacOSH264Source"]


def _apply_common_config(source: CameraSource, config: dict):
    """Apply common config parameters to any camera source."""
    if "idle_fps" in config:
        source._target_fps = config["idle_fps"]
    if "fps" in config:
        source._active_fps = config["fps"]
    if "h264_bitrate" in config and hasattr(source, '_h264_bitrate'):
        source._h264_bitrate = config["h264_bitrate"]
    if "detection_cooldown" in config and hasattr(source, '_detection_cooldown'):
        source._detection_cooldown = config["detection_cooldown"]


def create_camera_source(config: dict) -> CameraSource:
    """Factory function to create appropriate camera source based on config."""
    source_type = config["camera_source"]
    width = config["frame_width"]
    height = config["frame_height"]
    camera_id = config["camera_id"]
    
    if source_type == "macos":
        source = MacOSSource(width, height, camera_id)
        if "motion_threshold" in config:
            source._motion_threshold = config["motion_threshold"]
        if "min_motion_area" in config:
            source._min_motion_area = config["min_motion_area"]
    elif source_type == "rpi_ai":
        source = RPiAISource(width, height, camera_id)
    elif source_type == "rpi":
        rotation = config.get("camera_rotation", 0)
        source = RPiRegularSource(width, height, camera_id, rotation)
        if "motion_threshold" in config:
            source._motion_threshold = config["motion_threshold"]
        if "min_motion_area" in config:
            source._min_motion_area = config["min_motion_area"]
    elif source_type == "macos_h264":
        rotation = config.get("camera_rotation", 0)
        source = MacOSH264Source(width, height, camera_id, rotation)
        if "motion_threshold" in config:
            source._motion_threshold = config["motion_threshold"]
        if "min_motion_area" in config:
            source._min_motion_area = config["min_motion_area"]
    elif source_type == "synthetic":
        source = SyntheticSource(width, height, camera_id)
    else:
        raise ValueError(f"Unknown camera source: {source_type}")
    
    _apply_common_config(source, config)
    return source
