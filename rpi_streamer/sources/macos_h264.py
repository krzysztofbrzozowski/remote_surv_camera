"""macOS camera with H.264 encoding via ffmpeg VideoToolbox.

Mirrors the RPi H.264 architecture:
- OpenCV capture → ffmpeg H.264 encoder → chunked NAL output
- Motion detection on captured frames
- Dynamic FPS via frame feed rate control
- Timestamp overlay via OpenCV pre-encode
"""
import logging
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np

from .base import CameraSource

logger = logging.getLogger(__name__)

# Annex B start code
_START_CODE = b'\x00\x00\x00\x01'


class MacOSH264Source(CameraSource):
    """macOS webcam → H.264 via ffmpeg (VideoToolbox HW or libx264 SW)."""

    def __init__(self, width: int, height: int, camera_id: str, rotation: int = 0):
        super().__init__(width, height, camera_id)
        self.rotation = rotation
        self._h264_bitrate = 4_000_000
        self._iperiod = 10
        self._cap = None
        self._ffmpeg = None
        self._reader_thread = None
        self._feeder_thread = None
        self._chunks = deque(maxlen=120)
        self._running = False
        self._current_fps = 1.0
        self._last_encode_time = 0.0
        self._last_send_time = 0.0

        # Motion detection state
        self._prev_gray = None
        self._motion_threshold = 25
        self._min_motion_area = 500
        self._detection_cooldown = 1.5
        self._last_motion_time = 0
        self._motion_history = []
        self._frame_count = 0

        # Frame queue for encoder feed
        self._frame_queue = deque(maxlen=4)
        self._frame_lock = threading.Lock()

    @property
    def encoding_type(self) -> str:
        return "h264"

    def open(self):
        # Swap dimensions for 90/270 rotation (encode in portrait, rotate in CSS)
        if self.rotation in (90, 270):
            enc_w, enc_h = self.height, self.width
        else:
            enc_w, enc_h = self.width, self.height

        self._enc_w = enc_w
        self._enc_h = enc_h

        self._cap = cv2.VideoCapture(0)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if not self._cap.isOpened():
            raise RuntimeError("Could not open macOS camera")

        actual_w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(f"macOS camera: capture {actual_w}x{actual_h}")

        self._start_ffmpeg(enc_w, enc_h)
        self._running = True

        self._reader_thread = threading.Thread(target=self._read_h264, daemon=True)
        self._reader_thread.start()

        self._feeder_thread = threading.Thread(target=self._feed_loop, daemon=True)
        self._feeder_thread.start()

        logger.info(
            f"macOS H.264 opened at {enc_w}x{enc_h} "
            f"(rotation: {self.rotation}°, bitrate: {self._h264_bitrate})"
        )

    def _start_ffmpeg(self, w: int, h: int):
        """Start ffmpeg subprocess for H.264 encoding."""
        # Try VideoToolbox first, fall back to libx264
        for codec in ['h264_videotoolbox', 'libx264']:
            extra = []
            if codec == 'libx264':
                extra = ['-preset', 'ultrafast', '-tune', 'zerolatency']
            cmd = [
                'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                '-s', f'{w}x{h}', '-r', '30',
                '-i', 'pipe:0',
                '-c:v', codec, *extra,
                '-profile:v', 'baseline',
                '-b:v', str(self._h264_bitrate),
                '-maxrate', str(int(self._h264_bitrate * 1.5)),
                '-bufsize', str(int(self._h264_bitrate * 2)),
                '-g', str(self._iperiod),
                '-bf', '0',
                '-f', 'h264', '-an', 'pipe:1'
            ]
            try:
                self._ffmpeg = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                logger.info(f"H.264 encoder: {codec}")
                return
            except Exception as e:
                logger.warning(f"{codec} failed: {e}")
                continue
        raise RuntimeError("No H.264 encoder available (need ffmpeg with h264_videotoolbox or libx264)")

    def _feed_loop(self):
        """Feed frames to ffmpeg at a steady ~30fps to avoid encoder stalls.

        Motion detection runs on every frame. FPS gating happens on the
        output side (get_encoded_chunks only releases at target rate).
        """
        while self._running:
            t0 = time.time()

            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.01)
                continue

            # Motion detection on every frame
            self._detect_motion(frame)

            # Rotate if needed
            if self.rotation == 90:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
            elif self.rotation == 180:
                frame = cv2.rotate(frame, cv2.ROTATE_180)
            elif self.rotation == 270:
                frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

            # Timestamp overlay
            self._draw_timestamp(frame)

            self._frame_count += 1

            try:
                self._ffmpeg.stdin.write(frame.tobytes())
                self._ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError):
                logger.error("ffmpeg stdin pipe broken")
                self._running = False
                break

            # Steady ~30fps feed to keep encoder pipeline full
            elapsed = time.time() - t0
            sleep_time = max(0, (1.0 / 30.0) - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _draw_timestamp(self, frame: np.ndarray):
        """Burn timestamp into frame (matches RPi pre_callback style)."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        h, w = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = max(0.5, w / 1600.0)
        thickness = max(1, int(scale * 2))
        (tw, th), _ = cv2.getTextSize(ts, font, scale, thickness)
        x, y = w - tw - 10, th + 10
        cv2.putText(frame, ts, (x, y), font, scale, (0, 0, 0), thickness + 2)
        cv2.putText(frame, ts, (x, y), font, scale, (0, 255, 255), thickness)

    def _detect_motion(self, frame: np.ndarray):
        """Detect motion by frame differencing (matches RPi source logic)."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (21, 21), 0)

        if self._prev_gray is None:
            self._prev_gray = gray
            return

        delta = cv2.absdiff(self._prev_gray, gray)
        thresh = cv2.threshold(delta, self._motion_threshold, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=2)
        motion_pixels = int(np.sum(thresh > 0))
        self._prev_gray = gray

        self._motion_history.append(motion_pixels)
        if len(self._motion_history) > 30:
            self._motion_history.pop(0)

        was_active = self._detection_active
        if motion_pixels > self._min_motion_area:
            self._detection_active = True
            self._last_motion_time = time.time()
            if not was_active:
                avg = np.mean(self._motion_history[-10:]) if len(self._motion_history) >= 10 else motion_pixels
                logger.info(f"🔴 Motion detected: {motion_pixels} pixels (avg: {avg:.0f})")
        else:
            elapsed = time.time() - self._last_motion_time if self._last_motion_time > 0 else 999
            if self._detection_active and elapsed > self._detection_cooldown:
                self._detection_active = False
                logger.info(f"🟢 Motion ended (cooldown: {elapsed:.1f}s)")

    def _read_h264(self):
        """Read H.264 from ffmpeg, emit complete access units.

        Accumulates NAL units and emits complete frames (access units):
        - Keyframe AU = SPS + PPS + SEI + IDR slice
        - P-frame AU  = single non-IDR slice NAL
        Each emitted chunk starts with Annex B start code for clean jmuxer decoding.
        """
        buf = b''
        pending_au = b''
        pending_has_vcl = False
        pending_is_kf = False

        while self._running:
            try:
                data = self._ffmpeg.stdout.read(4096)
            except Exception:
                break
            if not data:
                break
            buf += data

            # Extract and process complete NAL units
            while True:
                sc1 = buf.find(_START_CODE)
                if sc1 == -1:
                    buf = b''
                    break
                if sc1 > 0:
                    buf = buf[sc1:]
                sc2 = buf.find(_START_CODE, 4)
                if sc2 == -1:
                    break  # incomplete NAL, wait for more data

                nal = buf[:sc2]
                buf = buf[sc2:]
                nal_type = nal[4] & 0x1f

                if nal_type == 7:
                    # SPS → emit previous AU and start new keyframe AU
                    if pending_au and pending_has_vcl:
                        self._chunks.append({
                            "data": pending_au,
                            "keyframe": pending_is_kf,
                        })
                    pending_au = nal
                    pending_has_vcl = False
                    pending_is_kf = True
                elif nal_type in (1, 5) and pending_has_vcl:
                    # New slice when current AU already has one → new frame
                    self._chunks.append({
                        "data": pending_au,
                        "keyframe": pending_is_kf,
                    })
                    pending_au = nal
                    pending_has_vcl = True
                    pending_is_kf = (nal_type == 5)
                else:
                    # PPS, SEI, or first slice of current AU — append
                    pending_au += nal
                    if nal_type in (1, 5):
                        pending_has_vcl = True
                        if nal_type == 5:
                            pending_is_kf = True

        # Emit remaining AU
        if pending_au and pending_has_vcl:
            self._chunks.append({"data": pending_au, "keyframe": pending_is_kf})

    # ---- CameraSource interface ----------------------------------------
    def check_motion(self):
        """Motion detection runs in feeder thread; this is a no-op."""
        pass

    def get_encoded_chunks(self) -> list:
        """Return pending access units, gated to target FPS.

        At low FPS: accumulates AUs and returns only the latest one per
        interval (preferring keyframes). At high FPS: returns everything.
        On burst transition, trim to start from latest keyframe.
        """
        if not self._chunks:
            return []

        now = time.time()
        interval = 1.0 / max(self._current_fps, 0.5)

        if self._current_fps >= 20:
            # High FPS: forward everything
            result = list(self._chunks)
            self._chunks.clear()
            self._last_send_time = now
            # On burst from idle→active, start from latest keyframe
            if len(result) > 5:
                for i in range(len(result) - 1, -1, -1):
                    if result[i].get("keyframe") or result[i]["keyframe"]:
                        result = result[i:]
                        break
            return result

        # Low FPS: check if enough time has passed
        if now - self._last_send_time < interval:
            return []

        # Time to send — take latest AU (prefer keyframe)
        all_aus = list(self._chunks)
        self._chunks.clear()
        self._last_send_time = now

        keyframes = [c for c in all_aus if c["keyframe"]]
        return keyframes[-1:] if keyframes else all_aus[-1:]

    def set_fps(self, fps: float):
        self._current_fps = fps

    def get_detection_info(self) -> dict:
        elapsed = time.time() - self._last_motion_time if self._last_motion_time > 0 else 0
        avg = np.mean(self._motion_history) if self._motion_history else 0
        display_rotation = self.rotation if self.rotation in (90, 270) else 0
        return {
            "detection_active": self._detection_active,
            "motion_detection": True,
            "motion_pixels": int(self._motion_history[-1]) if self._motion_history else 0,
            "avg_motion_pixels": int(avg),
            "seconds_since_motion": round(elapsed, 1),
            "cooldown_remaining": max(0, self._detection_cooldown - elapsed)
                if self._detection_active else 0,
            "frame_count": self._frame_count,
            "display_rotation": display_rotation,
        }

    def grab_frame(self) -> np.ndarray:
        """Fallback for JPEG path (not used in H.264 mode)."""
        ret, frame = self._cap.read()
        if not ret:
            raise RuntimeError("Frame grab failed")
        return frame

    def close(self):
        self._running = False
        if self._ffmpeg:
            try:
                self._ffmpeg.stdin.close()
            except Exception:
                pass
            self._ffmpeg.wait(timeout=5)
        if self._cap:
            self._cap.release()
        logger.info("macOS H.264 source closed")
