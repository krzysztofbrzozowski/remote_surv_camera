"""Raspberry Pi regular camera (Camera Module 3) — H.264 encoding with motion detection."""
import time
import logging
from collections import deque
from datetime import datetime
import numpy as np
import cv2
from .base import CameraSource

logger = logging.getLogger(__name__)


class RPiRegularSource(CameraSource):
    """Raspberry Pi regular camera via picamera2 with H.264 hardware encoding.

    Uses create_video_configuration() for a video-optimised capture pipeline.
    H.264 is encoded by the RPi's hardware encoder (V4L2 M2M).
    Motion detection runs on the low-resolution (lores) stream so it
    never competes with the encoder for the main buffer.

    Supports:
    - Optional rotation (0, 90, 180, 270 degrees — 180° handled in HW)
    - Dynamic FPS switching via FrameDurationLimits
    """

    def __init__(self, width: int, height: int, camera_id: str, rotation: int = 0):
        super().__init__(width, height, camera_id)
        self.picam = None
        self.encoder = None
        self._stream_output = None
        self.rotation = rotation

        if self.rotation not in [0, 90, 180, 270]:
            logger.warning(f"Invalid rotation {self.rotation}, using 0°")
            self.rotation = 0

        # For 90°/270° the sensor captures in swapped dimensions
        if self.rotation in [90, 270]:
            self.capture_width = height
            self.capture_height = width
        else:
            self.capture_width = width
            self.capture_height = height

        # H.264 encoding defaults (overridden by config via factory)
        self._h264_bitrate = 2_000_000  # 2 Mbps

        # FPS gating (mirrors macos_h264 approach for burst prevention)
        self._current_fps = 1.0
        self._last_send_time = 0.0
        self._pending_chunks: deque = deque(maxlen=30)
        self._send_has_keyframe: bool = False

        # Motion detection (runs on 640×480 lores stream)
        self._prev_gray = None
        self._motion_threshold = 25
        self._min_motion_area = 500
        self._detection_cooldown = 3.0
        self._last_motion_time = 0
        self._last_motion_check = 0

        self._frame_count = 0
        self._motion_history = []
        self._history_size = 30

    # ------------------------------------------------------------------
    @property
    def encoding_type(self) -> str:
        return "h264"

    # ------------------------------------------------------------------
    def open(self):
        try:
            from picamera2 import Picamera2
            from picamera2.encoders import H264Encoder
            from .h264_output import StreamOutput
            from libcamera import Transform

            self.picam = Picamera2()

            # 180° is the only rotation libcamera can do purely in HW
            transform = Transform(hflip=True, vflip=True) if self.rotation == 180 else Transform()

            # Force 2304x1296 sensor mode so the IMX708 can achieve 25+ FPS.
            # The default full-res mode (4608x2592) maxes out at ~14 FPS.
            config = self.picam.create_video_configuration(
                main={"size": (self.capture_width, self.capture_height)},
                lores={"size": (640, 480), "format": "YUV420"},
                sensor={"output_size": (2304, 1296)},
                transform=transform,
                buffer_count=6,
            )
            self.picam.configure(config)

            # Sensor always at max FPS — output gating in software eliminates
            # the 1-3s hardware ramp-up when switching from idle to active.
            frame_dur = int(1_000_000 / self._active_fps)
            self.picam.set_controls({"FrameDurationLimits": (frame_dur, frame_dur)})

            # H.264 hardware encoder
            self.encoder = H264Encoder(
                bitrate=self._h264_bitrate,
                repeat=True,
                iperiod=15,
            )
            self._stream_output = StreamOutput()

            # Burn timestamp into each frame before encoding
            self.picam.pre_callback = self._draw_timestamp

            self.picam.start()
            self.picam.start_encoder(self.encoder, self._stream_output)

            logger.info(
                f"RPi Camera (H.264) opened at {self.capture_width}x{self.capture_height} "
                f"(rotation: {self.rotation}°)"
            )
            logger.info(
                f"H.264 encoding: bitrate={self._h264_bitrate / 1e6:.1f} Mbps, iperiod=15"
            )
            logger.info(f"Motion detection on lores (640×480): threshold={self._motion_threshold}, min_area={self._min_motion_area}")

        except ImportError as e:
            logger.error(f"Failed to import picamera2: {e}")
            raise RuntimeError("picamera2 required for RPi Camera. Install with: pip install picamera2")
        except Exception as e:
            logger.error(f"Failed to initialise RPi Camera: {e}")
            raise

    def close(self):
        if self.picam:
            try:
                self.picam.stop_encoder()
                self.picam.stop()
                self.picam.close()
            except Exception:
                pass

    # ---- FPS control -------------------------------------------------
    def set_fps(self, fps: float):
        """Update output gating rate. Sensor stays at max FPS for instant transitions."""
        old_fps = self._current_fps
        self._current_fps = fps

        if abs(fps - old_fps) > 1.0:
            # Force the encoder to produce an immediate keyframe so
            # jmuxer gets a clean IDR right at the transition instead
            # of waiting up to 600ms (iperiod=15 at 25fps).
            if self.encoder:
                try:
                    self.encoder.force_key_frame()
                except Exception:
                    pass

            if fps < old_fps:
                # HIGH→LOW (active→idle): force immediate keyframe send
                # by resetting _last_send_time so the low-FPS interval
                # check passes on the very next poll.
                self._last_send_time = 0
            else:
                # LOW→HIGH (idle→active): reset keyframe flag so the
                # first burst starts from a clean IDR (prevents orphan
                # P-frames that cause black video).
                self._send_has_keyframe = False

        logger.debug(f"Output rate → {fps} FPS (sensor always at {self._active_fps})")

    # ---- Timestamp overlay -------------------------------------------
    def _draw_timestamp(self, request):
        """Burn timestamp into the main stream before H.264 encoding."""
        from picamera2 import MappedArray
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with MappedArray(request, "main") as m:
            frame = m.array
            h, w = frame.shape[:2]
            font = cv2.FONT_HERSHEY_SIMPLEX
            scale = max(0.5, w / 1200.0)
            thickness = max(1, int(scale * 2))
            (tw, th), _ = cv2.getTextSize(ts, font, scale, thickness)
            x, y = w - tw - 10, th + 10
            cv2.putText(frame, ts, (x, y), font, scale, (0, 0, 0, 255), thickness + 2)
            cv2.putText(frame, ts, (x, y), font, scale, (0, 255, 255, 255), thickness)

    # ---- H.264 chunk access ------------------------------------------
    def get_encoded_chunks(self) -> list:
        """Return pending H.264 access units, gated to target FPS.

        Sensor always runs at max FPS. At high output FPS, forward everything.
        At low output FPS, gate to one chunk per interval — keyframes ONLY
        to prevent orphan P-frames that cause browser blackouts.

        The _send_has_keyframe flag tracks whether a keyframe has been sent
        in the current FPS epoch. It resets on FPS transitions so the first
        chunk after a transition is always a keyframe (SPS+PPS+IDR).
        """
        if not self._stream_output:
            return []

        # Drain new chunks from encoder into pending buffer
        new_chunks = self._stream_output.get_chunks()
        self._pending_chunks.extend(new_chunks)

        if not self._pending_chunks:
            return []

        now = time.time()

        if self._current_fps >= 20:
            # High FPS: forward everything
            result = list(self._pending_chunks)
            self._pending_chunks.clear()

            if not self._send_has_keyframe:
                # First burst after transition or start — trim to start
                # from latest keyframe so decoder gets a clean reference
                for i in range(len(result) - 1, -1, -1):
                    if result[i].get("keyframe"):
                        result = result[i:]
                        self._send_has_keyframe = True
                        break
                else:
                    # No keyframe yet — hold chunks until one arrives
                    self._pending_chunks.extend(result)
                    return []

            self._last_send_time = now
            return result

        # Low FPS: gate to one chunk per interval — KEYFRAMES ONLY
        # Sending lone P-frames causes black video in the browser.
        interval = 1.0 / max(self._current_fps, 0.5)
        time_since_send = now - self._last_send_time

        if time_since_send < interval:
            return []

        all_chunks = list(self._pending_chunks)
        self._pending_chunks.clear()

        keyframes = [c for c in all_chunks if c.get("keyframe")]
        if keyframes:
            self._last_send_time = now
            self._send_has_keyframe = True
            # Force next keyframe immediately so it's ready well
            # before the next send window (~333ms away at 3 fps).
            if self.encoder:
                try:
                    self.encoder.force_key_frame()
                except Exception:
                    pass
            return keyframes[-1:]

        # No keyframe available — force and wait for next poll
        if self.encoder:
            try:
                self.encoder.force_key_frame()
            except Exception:
                pass
        return []

    # ---- Motion detection on lores -----------------------------------
    def check_motion(self):
        now = time.time()
        min_interval = 0.1 if self._detection_active else 0.2
        if now - self._last_motion_check < min_interval:
            return
        self._last_motion_check = now

        try:
            lores = self.picam.capture_array("lores")
        except Exception:
            return

        # YUV420: Y plane is first (h × w) block — already grayscale
        h, w = 480, 640
        gray = lores[:h, :w] if lores.ndim == 2 else cv2.cvtColor(lores[:h, :w], cv2.COLOR_RGB2GRAY) if lores.ndim == 3 else lores[:h, :w]
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
        if len(self._motion_history) > self._history_size:
            self._motion_history.pop(0)

        motion_detected = motion_pixels > self._min_motion_area
        was_detecting = self._detection_active

        if motion_detected:
            self._detection_active = True
            self._last_motion_time = time.time()
            if not was_detecting:
                avg = np.mean(self._motion_history[-10:]) if len(self._motion_history) >= 10 else motion_pixels
                logger.info(f"🔴 Motion detected: {motion_pixels} pixels (avg: {avg:.0f})")
        else:
            elapsed = time.time() - self._last_motion_time
            if self._detection_active and elapsed > self._detection_cooldown:
                self._detection_active = False
                logger.info(f"🟢 Motion ended (cooldown: {elapsed:.1f}s)")

        self._frame_count += 1

    # ---- grab_frame (backward compat; not used in H.264 path) --------
    def grab_frame(self) -> np.ndarray:
        return self.picam.capture_array("lores")

    # ---- Detection metadata ------------------------------------------
    def get_detection_info(self) -> dict:
        elapsed = time.time() - self._last_motion_time if self._last_motion_time > 0 else 0
        avg_motion = np.mean(self._motion_history) if self._motion_history else 0
        # For 90°/270°, viewer needs CSS rotation (HW only handles 180°)
        display_rotation = self.rotation if self.rotation in [90, 270] else 0
        return {
            "detection_active": self._detection_active,
            "motion_detection": True,
            "motion_pixels": int(self._motion_history[-1]) if self._motion_history else 0,
            "avg_motion_pixels": int(avg_motion),
            "seconds_since_motion": round(elapsed, 1),
            "cooldown_remaining": max(0, self._detection_cooldown - elapsed) if self._detection_active else 0,
            "frame_count": self._frame_count,
            "rotation": self.rotation,
            "display_rotation": display_rotation,
        }
