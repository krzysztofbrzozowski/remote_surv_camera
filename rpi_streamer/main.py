#!/usr/bin/env python3
"""RPi Camera Streamer - streams camera feed + metadata to Django server via WebSocket.

Camera sources (from sources/ module):
  - "synthetic" : generated test pattern (default, works everywhere)
  - "macos"     : macOS built-in camera via OpenCV with simulated detection
  - "rpi"       : Raspberry Pi regular camera (Camera Module 3) — H.264 via picamera2
  - "rpi_ai"    : Raspberry Pi AI Camera (IMX500) with hardware detection

Encoding:
  - rpi source  : H.264 hardware encoding via V4L2 M2M
  - all others  : JPEG per-frame encoding

Dynamic FPS:
  - Idle (no detection): 1 FPS
  - Detection active:    config["fps"] (default 15-30)
"""

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import psutil

from sources import create_camera_source
from sources.ina219_power import INA219PowerMonitor

# Start daily logger (RX/TX tracking + daily log files)
try:
    from daily_logger import DailyLogger
    _daily_logger = DailyLogger()
    _daily_logger.start()
    _streamer_file_logger = _daily_logger.get_app_logger()
except Exception:
    _daily_logger = None
    _streamer_file_logger = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


DEFAULTS = {
    "server_url": "ws://localhost:8000/ws/camera/feed/",
    "camera_token": "",
    "camera_id": "cam-1",
    "camera_source": "synthetic",
    "frame_width": 1920,
    "frame_height": 1080,
    "fps": 30,            # Max FPS (used when detection active)
    "idle_fps": 1,        # FPS when no detection
    "jpeg_quality": 70,
    "h264_bitrate": 2_000_000,  # 2 Mbps default for H.264
}


def load_config():
    """Load config from YAML or JSON file.

    Search order:
      1. Path in STREAMER_CONFIG env var (if set)
      2. config.yaml in current directory
      3. config.json in current directory

    Raises FileNotFoundError if no config file is found.
    """
    config_path = os.environ.get("STREAMER_CONFIG", "")

    if config_path and Path(config_path).exists():
        return _load_file(config_path)

    if Path("config.yaml").exists():
        return _load_file("config.yaml")

    if Path("config.json").exists():
        return _load_file("config.json")

    raise FileNotFoundError(
        "No config file found. Create config.yaml (see config.example.yaml) "
        "or set STREAMER_CONFIG env var to the path."
    )


def _load_file(path: str) -> dict:
    with open(path) as f:
        content = f.read().strip()
    if not content:
        raise ValueError(f"Config file is empty: {path}")
    if path.endswith((".yaml", ".yml")):
        import yaml
        data = yaml.safe_load(content) or {}
    else:
        data = json.loads(content)
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if v is not None})

    # Load camera_token from configs/.token file if not set in config
    if not merged.get("camera_token"):
        token_file = Path(__file__).resolve().parent / "configs" / ".token"
        if token_file.is_file():
            token = token_file.read_text().strip()
            if token:
                merged["camera_token"] = token

    if not merged.get("camera_token"):
        raise ValueError(
            "camera_token is required. Set it in config file or in configs/.token"
        )
    return merged


class MetadataCollector:
    """Collects system metadata (WiFi signal, voltage, power, CPU, etc.)."""

    def __init__(self, camera_id: str, config: dict = None):
        self.camera_id = camera_id
        self._frame_count = 0
        self._bitrate_window = 2.0  # seconds
        self._byte_samples = []  # list of (timestamp, bytes)
        self._last_bitrate = 0.0
        self._total_bytes_sent = 0  # Total bytes sent since start
        self._power_monitor = INA219PowerMonitor()
        self._config = config or {}
        self._last_cpu = 0.0
        self._last_cpu_poll = 0
        self._cpu_poll_interval = 2.0

    def get_wifi_signal(self) -> int:
        try:
            with open("/proc/net/wireless") as f:
                lines = f.readlines()
                if len(lines) >= 3:
                    parts = lines[2].split()
                    return int(float(parts[3]))
        except (FileNotFoundError, IndexError, ValueError):
            pass
        return -45 + int(10 * np.sin(time.time() / 5))

    def get_voltage(self) -> float:
        # Prefer INA219 load voltage if available
        power_data = self._power_monitor.get_data()
        if power_data and "load_voltage_v" in power_data:
            return power_data["load_voltage_v"]

        try:
            import subprocess
            result = subprocess.run(
                ["vcgencmd", "measure_volts", "core"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0:
                return float(result.stdout.split("=")[1].strip().replace("V", ""))
        except (IndexError, ValueError, OSError, FileNotFoundError):
            pass
        except Exception:
            pass
        return round(4.9 + 0.2 * np.sin(time.time() / 10), 2)

    def update_bytes(self, n: int):
        self._byte_samples.append((time.time(), n))
        self._total_bytes_sent += n
        self._frame_count += 1

    def get_bitrate_kbps(self) -> float:
        now = time.time()
        cutoff = now - self._bitrate_window
        self._byte_samples = [(t, b) for t, b in self._byte_samples if t >= cutoff]
        if not self._byte_samples:
            return self._last_bitrate
        elapsed = now - self._byte_samples[0][0]
        if elapsed < 0.5:
            return self._last_bitrate
        total_bytes = sum(b for _, b in self._byte_samples)
        self._last_bitrate = round((total_bytes * 8) / elapsed / 1000, 1)
        return self._last_bitrate

    def get_send_rate_mbps(self) -> float:
        """Calculate current send rate in MB/s (megabytes per second)."""
        now = time.time()
        cutoff = now - self._bitrate_window
        recent_samples = [(t, b) for t, b in self._byte_samples if t >= cutoff]
        if not recent_samples:
            return 0.0
        elapsed = now - recent_samples[0][0]
        if elapsed < 0.5:
            return 0.0
        total_bytes = sum(b for _, b in recent_samples)
        return round(total_bytes / elapsed / (1024 * 1024), 3)  # Convert to MB/s

    def get_total_mb_sent(self) -> float:
        """Get total megabytes sent since start."""
        return round(self._total_bytes_sent / (1024 * 1024), 2)

    def get_cpu_percent(self) -> float:
        now = time.time()
        if now - self._last_cpu_poll >= self._cpu_poll_interval:
            self._last_cpu = psutil.cpu_percent(interval=0)
            self._last_cpu_poll = now
        return self._last_cpu

    def get_metadata(self) -> dict:
        encoding = self._config.get("_encoding", "jpeg")
        meta = {
            "camera_id": self.camera_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "wifi_signal_dbm": self.get_wifi_signal(),
            "voltage_v": self.get_voltage(),
            "bitrate_kbps": self.get_bitrate_kbps(),
            "send_rate_mbps": self.get_send_rate_mbps(),
            "total_mb_sent": self.get_total_mb_sent(),
            "frame_count": self._frame_count,
            "cpu_percent": self.get_cpu_percent(),
            "resolution": f"{self._config.get('frame_width', '?')}x{self._config.get('frame_height', '?')}",
            "encoding": encoding,
        }
        if encoding == "jpeg":
            meta["jpeg_quality"] = self._config.get("jpeg_quality", "?")
            meta["color_space"] = "YUV420 → BGR → JPEG"
        else:
            meta["h264_bitrate"] = self._config.get("h264_bitrate", "?")
            meta["color_space"] = "YUV420 → H.264"
        
        # Add INA219 power monitoring data if available
        power_data = self._power_monitor.get_data()
        if power_data:
            meta["power"] = power_data
        
        return meta


class Streamer:
    """WebSocket streamer — supports both H.264 and JPEG encoding.
    
    Encoding mode is determined by the camera source:
    - rpi source  → H.264 (hardware encoder, chunks from StreamOutput)
    - all others  → JPEG  (per-frame OpenCV encoding)
    
    Dynamic FPS switching:
    - 1 FPS when idle (no detection)
    - config["fps"] when detection active
    """

    def __init__(self, config: dict):
        self.config = config
        self.camera = create_camera_source(config)
        self.jpeg_quality = config["jpeg_quality"]
        # Store encoding type so MetadataCollector can reference it
        config["_encoding"] = self.camera.encoding_type
        self.metadata = MetadataCollector(config["camera_id"], config)
        self.running = True
        
        # FPS configuration
        self.max_fps = config["fps"]
        self.idle_fps = config.get("idle_fps", 1)
        self.current_fps = self.idle_fps
        self.frame_interval = 1.0 / self.current_fps

    def _burn_timestamp(self, frame: np.ndarray) -> np.ndarray:
        """Burn current timestamp into the frame."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = max(0.4, w / 1600.0)
        thickness = max(1, int(scale * 2))
        (tw, th), _ = cv2.getTextSize(ts, font, scale, thickness)
        x, y = w - tw - 10, th + 10
        cv2.putText(frame, ts, (x, y), font, scale, (0, 0, 0), thickness + 2)
        cv2.putText(frame, ts, (x, y), font, scale, (0, 255, 255), thickness)
        return frame

    def encode_jpeg(self, frame: np.ndarray) -> bytes:
        frame = self._burn_timestamp(frame)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        return buf.tobytes()

    # ---- FPS management ----------------------------------------------
    def _update_fps(self):
        """Update FPS based on detection state (works for both modes)."""
        target_fps = self.camera.get_target_fps()
        if target_fps != self.current_fps:
            self.current_fps = target_fps
            if self.camera.encoding_type == "h264":
                self.camera.set_fps(target_fps)
            else:
                self.frame_interval = 1.0 / self.current_fps
            logger.info(f"📹 FPS switched to {self.current_fps}")

    # ---- Main entry ---------------------------------------------------
    async def run(self):
        import websockets

        self.camera.open()
        url = self.config["server_url"]
        token = self.config["camera_token"]
        camera_id = self.config["camera_id"]
        ws_url = f"{url}?camera_id={camera_id}"
        enc = self.camera.encoding_type.upper()

        while self.running:
            try:
                logger.info(f"Connecting to {ws_url}")
                async with websockets.connect(
                    ws_url,
                    additional_headers={"Authorization": f"Token {token}"},
                    max_size=10 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=60,
                ) as ws:
                    logger.info(f"Connected as [{camera_id}]! {enc} at {self.current_fps} FPS...")

                    if self.camera.encoding_type == "h264":
                        await self._stream_h264(ws)
                    else:
                        await self._stream_jpeg(ws)

            except Exception as e:
                logger.error(f"Connection error: {e}")
                if self.running:
                    logger.info("Reconnecting in 3s...")
                    await asyncio.sleep(3)

        self.camera.close()

    # ---- H.264 streaming loop ----------------------------------------
    async def _stream_h264(self, ws):
        """Poll encoder for chunks; motion detection on lores stream.

        Metadata is sent only on keyframes, FPS transitions, and every ~1s
        to minimise ws.send() overhead (halves the number of sends).
        """
        prev_fps = self.current_fps
        last_meta_time = 0.0
        META_INTERVAL = 1.0

        while self.running:
            self.camera.check_motion()
            self._update_fps()

            now = time.time()
            need_meta = False
            meta_extras = {}

            # On FPS transition, send metadata immediately
            if self.current_fps != prev_fps:
                need_meta = True
                if self.current_fps > prev_fps:
                    meta_extras["fps_boosted"] = True
            # Periodic metadata for viewer status updates
            elif now - last_meta_time >= META_INTERVAL:
                need_meta = True

            prev_fps = self.current_fps

            chunks = self.camera.get_encoded_chunks()
            for chunk in chunks:
                self.metadata.update_bytes(len(chunk["data"]))

                # Always send metadata before keyframes (server caches them)
                if chunk["keyframe"]:
                    need_meta = True
                    meta_extras["keyframe"] = True

                if need_meta:
                    meta = self.metadata.get_metadata()
                    meta.update(self.camera.get_detection_info())
                    meta["current_fps"] = self.current_fps
                    meta.update(meta_extras)
                    await ws.send(json.dumps({"type": "metadata", **meta}))
                    last_meta_time = now
                    need_meta = False
                    meta_extras = {}

                await ws.send(chunk["data"])

            # Send standalone metadata if due but no chunks this iteration
            if need_meta:
                meta = self.metadata.get_metadata()
                meta.update(self.camera.get_detection_info())
                meta["current_fps"] = self.current_fps
                meta.update(meta_extras)
                await ws.send(json.dumps({"type": "metadata", **meta}))
                last_meta_time = now

            # Faster polling at high FPS to keep up with encoder output
            poll_sleep = 0.005 if self.current_fps >= 20 else 0.05
            await asyncio.sleep(poll_sleep)

    # ---- JPEG streaming loop -----------------------------------------
    async def _stream_jpeg(self, ws):
        """Classic per-frame JPEG encoding (synthetic, macOS, rpi_ai)."""
        while self.running:
            t0 = time.time()

            frame = self.camera.grab_frame()
            self._update_fps()

            jpeg_data = self.encode_jpeg(frame)
            self.metadata.update_bytes(len(jpeg_data))
            meta = self.metadata.get_metadata()
            meta.update(self.camera.get_detection_info())
            meta["current_fps"] = self.current_fps

            await ws.send(json.dumps({"type": "metadata", **meta}))
            await ws.send(jpeg_data)

            elapsed = time.time() - t0
            await asyncio.sleep(max(0, self.frame_interval - elapsed))

    def stop(self):
        self.running = False


def main():
    config = load_config()
    from daily_logger import sanitize
    sanitized_config = sanitize(json.dumps(config, indent=2))
    logger.info(f"Config: {sanitized_config}")
    if _streamer_file_logger:
        _streamer_file_logger.info(f"Streamer started with config: {sanitized_config}")

    streamer = Streamer(config)

    def handle_signal(sig, frame):
        logger.info("Shutting down...")
        streamer.stop()
        if _daily_logger:
            _daily_logger.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(streamer.run())


if __name__ == "__main__":
    main()
