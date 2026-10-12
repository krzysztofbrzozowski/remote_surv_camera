#!/usr/bin/env python3
"""
Stream Diagnostics Tool
=======================
Connects to the surveillance camera WebSocket stream and analyzes frames
for glitches: duplicate/repeated frames, stuttering, timing irregularities,
and visual corruption.

Usage:
    python stream_diagnostics.py --host 192.0.2.11 --user admin --password admin
    python stream_diagnostics.py --host 192.0.2.11 --user admin --password admin --duration 60
    python stream_diagnostics.py --host 192.0.2.11 --user admin --password admin --save-frames
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

try:
    import numpy as np
except ImportError:
    sys.exit("Missing numpy. Install: pip install numpy")

try:
    from PIL import Image
except ImportError:
    sys.exit("Missing Pillow. Install: pip install Pillow")

try:
    import requests
except ImportError:
    sys.exit("Missing requests. Install: pip install requests")

try:
    import websocket
except ImportError:
    sys.exit("Missing websocket-client. Install: pip install websocket-client")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GlitchEvent:
    """A detected anomaly in the stream."""
    frame_seq: int
    timestamp: float
    kind: str            # duplicate | repeat_burst | stutter | gap | corrupt | frozen
    detail: str


@dataclass
class MetaSnapshot:
    """A snapshot of streamer metadata at a point in time."""
    timestamp: float
    current_fps: float = 0
    bitrate_kbps: float = 0
    h264_bitrate: float = 0
    send_rate_mbps: float = 0
    wifi_signal_dbm: float = 0
    cpu_percent: float = 0
    resolution: str = ""
    motion_pixels: int = 0
    detection_active: bool = False
    keyframe: bool = False
    frame_count: int = 0


@dataclass
class TransitionEvent:
    """Tracks an idle→active FPS transition and its decode health."""
    timestamp: float
    from_fps: float
    to_fps: float
    detection_active: bool = False         # motion state at transition
    first_keyframe_latency_ms: float = -1  # ms until first IDR after transition
    orphan_p_count: int = 0                # P-frames before first IDR
    chunks_before_keyframe: int = 0        # total chunks before first IDR
    blackout_risk: bool = False            # would jmuxer show black?


@dataclass
class DiagStats:
    """Accumulator for the entire diagnostic run."""
    start_time: float = 0.0
    total_frames: int = 0
    total_bytes: int = 0
    duplicates: int = 0
    repeat_bursts: int = 0
    gaps: int = 0
    corrupt: int = 0
    frozen_periods: int = 0
    keyframes: int = 0
    orphan_p_frames: int = 0          # P-frames before first keyframe
    blackout_windows: int = 0         # periods with no decodable keyframe
    keyframe_gaps: list = field(default_factory=list)  # time between keyframes
    decode_errors: int = 0            # ffmpeg decode errors
    h264_nal_types: dict = field(default_factory=dict)
    frame_intervals: list = field(default_factory=list)
    frame_sizes: list = field(default_factory=list)
    glitches: list = field(default_factory=list)
    fps_transitions: list = field(default_factory=list)
    transition_events: list = field(default_factory=list)  # TransitionEvent objects
    meta_snapshots: list = field(default_factory=list)

    def report(self) -> str:
        elapsed = time.monotonic() - self.start_time
        avg_fps = self.total_frames / elapsed if elapsed > 0 else 0

        intervals = self.frame_intervals
        if intervals:
            avg_dt = np.mean(intervals) * 1000
            std_dt = np.std(intervals) * 1000
            min_dt = np.min(intervals) * 1000
            max_dt = np.max(intervals) * 1000
            p95_dt = np.percentile(intervals, 95) * 1000
            p99_dt = np.percentile(intervals, 99) * 1000
            jitter = std_dt / avg_dt * 100 if avg_dt > 0 else 0
        else:
            avg_dt = std_dt = min_dt = max_dt = p95_dt = p99_dt = jitter = 0

        lines = [
            "",
            "=" * 70,
            "  STREAM DIAGNOSTICS REPORT",
            "=" * 70,
            f"  Duration        : {elapsed:.1f}s",
            f"  Total frames    : {self.total_frames}",
            f"  Total data      : {self.total_bytes / 1024 / 1024:.2f} MB",
            f"  Average FPS     : {avg_fps:.2f}",
            f"  Keyframes (IDR) : {self.keyframes}",
        ]

        if self.frame_sizes:
            avg_sz = np.mean(self.frame_sizes)
            max_sz = np.max(self.frame_sizes)
            lines.append(f"  Frame size      : avg={avg_sz/1024:.1f}KB  "
                         f"max={max_sz/1024:.1f}KB")

        lines += [
            "",
            "  Frame interval (ms):",
            f"    mean={avg_dt:.1f}  std={std_dt:.1f}  min={min_dt:.1f}  "
            f"max={max_dt:.1f}",
            f"    p95={p95_dt:.1f}  p99={p99_dt:.1f}  jitter={jitter:.1f}%",
        ]

        if self.h264_nal_types:
            lines.append("")
            lines.append("  H.264 NAL unit types seen:")
            for nal_type, count in sorted(self.h264_nal_types.items()):
                label = _NAL_TYPE_NAMES.get(nal_type, "unknown")
                lines.append(f"    type {nal_type:>2} ({label:<12}): {count}")

        if self.fps_transitions:
            lines.append("")
            lines.append("  FPS transitions:")
            for t, old, new in self.fps_transitions[-20:]:
                rel = t - self.start_time
                lines.append(f"    [{rel:7.2f}s] {old} → {new} fps")

        # Transition decode-health detail
        if self.transition_events:
            lines.append("")
            lines.append("  Transition decode health:")
            risky = 0
            for te in self.transition_events:
                rel = te.timestamp - self.start_time
                kf_lat = f"{te.first_keyframe_latency_ms:.0f}ms" if te.first_keyframe_latency_ms >= 0 else "NO KF"
                is_up = te.to_fps > te.from_fps
                direction = "↑" if is_up else "↓"
                risk = "⚠️  BLACKOUT RISK" if te.blackout_risk else "✅"
                if te.blackout_risk:
                    risky += 1
                lines.append(
                    f"    [{rel:7.2f}s] {direction} {te.from_fps}→{te.to_fps}fps  "
                    f"1st_kf={kf_lat}  orphan_p={te.orphan_p_count}  "
                    f"chunks={te.chunks_before_keyframe}  "
                    f"motion={te.detection_active}  {risk}"
                )
            if risky:
                lines.append(f"    → {risky}/{len(self.transition_events)} transitions have blackout risk")

        # Streamer metadata summary
        if self.meta_snapshots:
            last = self.meta_snapshots[-1]
            lines += [
                "",
                "  Last streamer metadata:",
                f"    reported fps  : {last.current_fps}",
                f"    bitrate       : {last.bitrate_kbps:.0f} kbps",
                f"    h264 target   : {last.h264_bitrate/1000:.0f} kbps",
                f"    send rate     : {last.send_rate_mbps:.2f} Mbps",
                f"    wifi signal   : {last.wifi_signal_dbm} dBm",
                f"    cpu           : {last.cpu_percent:.0f}%",
                f"    resolution    : {last.resolution}",
                f"    detection     : {last.detection_active}",
                f"    motion pixels : {last.motion_pixels}",
            ]

        lines += [
            "",
            "  Glitch summary:",
            f"    Exact duplicates      : {self.duplicates}",
            f"    Repeat bursts (3+)    : {self.repeat_bursts}",
            f"    Large gaps (>3× exp)  : {self.gaps}",
            f"    Corrupt frames        : {self.corrupt}",
            f"    Frozen periods (>2s)  : {self.frozen_periods}",
            f"    Orphan P-frames       : {self.orphan_p_frames}",
            f"    Blackout windows      : {self.blackout_windows}",
            f"    Decode errors         : {self.decode_errors}",
            f"    TOTAL glitch events   : {len(self.glitches)}",
        ]

        if self.keyframe_gaps:
            avg_kf = np.mean(self.keyframe_gaps) * 1000
            max_kf = np.max(self.keyframe_gaps) * 1000
            lines += [
                "",
                "  Keyframe spacing (ms):",
                f"    avg={avg_kf:.0f}  max={max_kf:.0f}  count={self.keyframes}",
            ]

        if self.glitches:
            lines.append("")
            lines.append("  Glitch log (last 50):")
            for g in self.glitches[-50:]:
                t = g.timestamp - self.start_time
                lines.append(
                    f"    [{t:7.2f}s] frame#{g.frame_seq:>5}  "
                    f"{g.kind:<16} {g.detail}"
                )

        lines.append("")
        # Weight decode/blackout issues more heavily
        critical = self.orphan_p_frames + self.blackout_windows + self.decode_errors
        transition_risks = sum(1 for te in self.transition_events if te.blackout_risk)
        score = len(self.glitches)
        if score == 0 and transition_risks == 0:
            verdict = "✅  STREAM LOOKS CLEAN"
        elif transition_risks > 0:
            verdict = (f"❌  TRANSITION BLACKOUT — {transition_risks} idle→active "
                       f"transitions sent P-frames before keyframe "
                       f"(browser shows black at FPS change)")
        elif critical > 0:
            verdict = ("❌  BLACKOUT RISK — orphan P-frames / decode errors "
                       "(browser will show black video)")
        elif score < 5:
            verdict = "⚠️   MINOR GLITCHES DETECTED"
        elif score < 20:
            verdict = "⚠️   MODERATE GLITCH LEVEL"
        else:
            verdict = "❌  SIGNIFICANT GLITCHES — NEEDS INVESTIGATION"
        lines.append(f"  Verdict: {verdict}")
        lines.append("=" * 70)
        return "\n".join(lines)


# H.264 NAL type names
_NAL_TYPE_NAMES = {
    1: "P-slice",
    2: "DPA",
    3: "DPB",
    4: "DPC",
    5: "IDR",
    6: "SEI",
    7: "SPS",
    8: "PPS",
    9: "AUD",
    12: "filler",
}


def parse_h264_nal_types(data: bytes) -> list:
    """Extract NAL unit types from Annex B bytestream."""
    nal_types = []
    i = 0
    while i < len(data) - 4:
        # Look for start codes: 0x00000001 or 0x000001
        if data[i:i+4] == b'\x00\x00\x00\x01':
            nal_byte = data[i+4]
            nal_types.append(nal_byte & 0x1F)
            i += 4
        elif data[i:i+3] == b'\x00\x00\x01':
            nal_byte = data[i+3]
            nal_types.append(nal_byte & 0x1F)
            i += 3
        else:
            i += 1
    return nal_types


# ---------------------------------------------------------------------------
# Frame analysis helpers
# ---------------------------------------------------------------------------

def decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    try:
        img = Image.open(io.BytesIO(data))
        return np.array(img)
    except Exception:
        return None


def frame_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def perceptual_hash(img_array: np.ndarray, hash_size: int = 16) -> str:
    """Fast average-hash: resize to small grayscale, threshold at mean."""
    img = Image.fromarray(img_array)
    img = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)
    pixels = np.array(img, dtype=np.float64)
    mean = pixels.mean()
    bits = (pixels > mean).flatten()
    hash_int = 0
    for b in bits:
        hash_int = (hash_int << 1) | int(b)
    return f"{hash_int:0{hash_size * hash_size // 4}x}"


def hamming_distance(h1: str, h2: str) -> int:
    i1, i2 = int(h1, 16), int(h2, 16)
    xor = i1 ^ i2
    return bin(xor).count("1")


def structural_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Simplified SSIM-like metric (0..1). 1 = identical."""
    if a.shape != b.shape:
        return 0.0
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    sig_a, sig_b = a.std(), b.std()
    sig_ab = ((a - mu_a) * (b - mu_b)).mean()
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    ssim = ((2 * mu_a * mu_b + C1) * (2 * sig_ab + C2)) / \
           ((mu_a ** 2 + mu_b ** 2 + C1) * (sig_a ** 2 + sig_b ** 2 + C2))
    return float(ssim)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def login(host: str, username: str, password: str, use_https: bool = False) -> str:
    scheme = "https" if use_https else "http"
    session = requests.Session()
    login_url = f"{scheme}://{host}/login/"

    resp = session.get(login_url, timeout=10)
    resp.raise_for_status()

    csrf_token = session.cookies.get("csrftoken", "")
    if not csrf_token:
        match = re.search(
            r'name=["\']csrfmiddlewaretoken["\']\s+value=["\']([^"\']+)',
            resp.text,
        )
        if match:
            csrf_token = match.group(1)
    if not csrf_token:
        sys.exit("Could not extract CSRF token from login page")

    resp = session.post(
        login_url,
        data={
            "username": username,
            "password": password,
            "csrfmiddlewaretoken": csrf_token,
        },
        headers={"Referer": login_url},
        timeout=10,
        allow_redirects=False,
    )

    sessionid = session.cookies.get("sessionid")
    if not sessionid:
        sys.exit(f"Login failed (status {resp.status_code}). Check credentials.")

    print(f"[auth] Logged in as '{username}'")
    return sessionid


# ---------------------------------------------------------------------------
# WebSocket stream analyzer
# ---------------------------------------------------------------------------

class StreamAnalyzer:
    def __init__(
        self,
        host: str,
        sessionid: str,
        camera_id: str = "cam-1",
        duration: float = 30.0,
        use_https: bool = False,
        save_frames: bool = False,
        save_dir: str = "diag_frames",
        verbose: bool = False,
    ):
        self.host = host
        self.sessionid = sessionid
        self.camera_id = camera_id
        self.duration = duration
        self.use_https = use_https
        self.save_frames = save_frames
        self.save_dir = save_dir
        self.verbose = verbose

        self.stats = DiagStats()
        self.encoding: str = "unknown"
        self.seq: int = 0
        self._last_meta: Optional[MetaSnapshot] = None
        self._reported_fps: Optional[float] = None

        self._recent_hashes: deque = deque(maxlen=120)
        self._recent_phashes: deque = deque(maxlen=120)
        self._prev_array: Optional[np.ndarray] = None
        self._prev_time: Optional[float] = None
        self._consecutive_dupes: int = 0
        self._frozen_since: Optional[float] = None
        self._h264_prev_chunk_hash: Optional[str] = None
        self._meta_printed: bool = False

        # --- Decode-state tracking (blackout detection) ---
        self._decoder_has_keyframe: bool = False   # has IDR been received?
        self._decoder_has_sps: bool = False        # SPS received?
        self._decoder_has_pps: bool = False        # PPS received?
        self._last_keyframe_time: Optional[float] = None
        self._orphan_p_since: Optional[float] = None  # first orphan P-frame time
        self._orphan_p_count: int = 0              # consecutive orphan P-frames
        self._blackout_start: Optional[float] = None
        self._fps_transition_pending: bool = False  # reset decode state on FPS change
        self._h264_stream_buf: bytearray = bytearray()  # for ffmpeg decode check
        self._decode_check_interval: float = 5.0
        self._last_decode_check: float = 0.0

        # --- Transition tracking (idle→active blackout detection) ---
        self._active_transition: Optional[TransitionEvent] = None  # in-flight transition
        self._transition_chunks: int = 0  # chunks since transition
        self._last_detection_active: Optional[bool] = None  # for motion state changes

        if save_frames:
            os.makedirs(save_dir, exist_ok=True)

    def _glitch(self, kind: str, detail: str):
        ev = GlitchEvent(
            frame_seq=self.seq,
            timestamp=time.monotonic(),
            kind=kind,
            detail=detail,
        )
        self.stats.glitches.append(ev)
        if kind == "duplicate":
            self.stats.duplicates += 1
        elif kind == "repeat_burst":
            self.stats.repeat_bursts += 1
        elif kind == "gap":
            self.stats.gaps += 1
        elif kind == "corrupt":
            self.stats.corrupt += 1
        elif kind == "frozen":
            self.stats.frozen_periods += 1
        elif kind == "orphan_p":
            self.stats.orphan_p_frames += 1
        elif kind == "blackout":
            self.stats.blackout_windows += 1
        elif kind == "decode_error":
            self.stats.decode_errors += 1

        if self.verbose:
            t = ev.timestamp - self.stats.start_time
            print(f"  ⚡ [{t:.2f}s] #{self.seq} {kind}: {detail}")

    def _process_jpeg_frame(self, data: bytes):
        md5 = frame_md5(data)
        arr = decode_jpeg(data)

        if arr is None:
            self._glitch("corrupt", f"JPEG decode failed ({len(data)} bytes)")
            if self.save_frames:
                path = os.path.join(self.save_dir, f"corrupt_{self.seq:06d}.jpg")
                with open(path, "wb") as f:
                    f.write(data)
            return

        phash = perceptual_hash(arr)

        # Exact duplicate check
        is_exact_dupe = md5 in self._recent_hashes

        # Perceptual near-duplicate check
        is_perceptual_dupe = False
        if self._recent_phashes:
            last_phash = self._recent_phashes[-1]
            dist = hamming_distance(phash, last_phash)
            is_perceptual_dupe = dist <= 3

        # SSIM vs previous
        ssim = None
        if self._prev_array is not None and arr.shape == self._prev_array.shape:
            ssim = structural_similarity(arr, self._prev_array)

        # Detect duplicates / frozen stream
        if is_exact_dupe:
            self._consecutive_dupes += 1
            self._glitch("duplicate", f"exact dup md5={md5[:12]}… "
                         f"(consecutive: {self._consecutive_dupes})")
            if self._frozen_since is None:
                self._frozen_since = time.monotonic()
        elif is_perceptual_dupe and ssim is not None and ssim > 0.998:
            self._consecutive_dupes += 1
            dist = hamming_distance(phash, self._recent_phashes[-1])
            self._glitch("duplicate", f"perceptual dup dist={dist} ssim={ssim:.4f} "
                         f"(consecutive: {self._consecutive_dupes})")
            if self._frozen_since is None:
                self._frozen_since = time.monotonic()
        else:
            if self._frozen_since is not None:
                frozen_dur = time.monotonic() - self._frozen_since
                if frozen_dur > 2.0:
                    self._glitch("frozen", f"stream frozen for {frozen_dur:.1f}s "
                                 f"({self._consecutive_dupes} dup frames)")
                self._frozen_since = None
            if self._consecutive_dupes >= 3:
                self._glitch("repeat_burst",
                             f"{self._consecutive_dupes} consecutive "
                             f"duplicate frames just ended")
            self._consecutive_dupes = 0

        # Timing analysis
        now = time.monotonic()
        if self._prev_time is not None:
            dt = now - self._prev_time
            self.stats.frame_intervals.append(dt)
            if len(self.stats.frame_intervals) > 10:
                recent = self.stats.frame_intervals[-30:]
                avg = np.mean(recent)
                if dt > max(avg * 3, 0.5):
                    self._glitch("gap", f"interval {dt*1000:.0f}ms "
                                 f"(avg {avg*1000:.0f}ms)")

        self._prev_time = now
        self._prev_array = arr
        self._recent_hashes.append(md5)
        self._recent_phashes.append(phash)

        if self.save_frames and (is_exact_dupe or is_perceptual_dupe):
            path = os.path.join(self.save_dir, f"dup_{self.seq:06d}.jpg")
            with open(path, "wb") as f:
                f.write(data)

    def _process_h264_chunk(self, data: bytes, is_keyframe: bool = False):
        md5 = frame_md5(data)
        now = time.monotonic()
        self.stats.frame_sizes.append(len(data))

        # Parse NAL units
        nal_types = parse_h264_nal_types(data)
        for nt in nal_types:
            self.stats.h264_nal_types[nt] = self.stats.h264_nal_types.get(nt, 0) + 1

        has_sps = 7 in nal_types
        has_pps = 8 in nal_types
        has_idr = 5 in nal_types
        has_p = 1 in nal_types

        # Snapshot before updating — used for gap detection
        had_keyframe_before = self._decoder_has_keyframe

        # ---- Decode-state / blackout detection ----
        if has_sps:
            self._decoder_has_sps = True
        if has_pps:
            self._decoder_has_pps = True

        if has_idr:
            self.stats.keyframes += 1
            # Track keyframe gap
            if self._last_keyframe_time is not None:
                kf_gap = now - self._last_keyframe_time
                self.stats.keyframe_gaps.append(kf_gap)
            self._last_keyframe_time = now

            # Check SPS/PPS preceded this IDR
            if not (self._decoder_has_sps and self._decoder_has_pps):
                self._glitch("corrupt",
                             f"IDR without SPS+PPS (sps={self._decoder_has_sps}, "
                             f"pps={self._decoder_has_pps})")

            # Mark decoder as having a valid reference
            self._decoder_has_keyframe = True

            # End orphan/blackout window if one was active
            if self._blackout_start is not None:
                dur = now - self._blackout_start
                self._glitch("blackout",
                             f"decoder had no keyframe for {dur*1000:.0f}ms "
                             f"({self._orphan_p_count} orphan P-frames)")
                self._blackout_start = None
            self._orphan_p_count = 0
            self._orphan_p_since = None

        elif has_p and not has_idr:
            if not self._decoder_has_keyframe:
                # P-frame without any preceding keyframe — decoder shows black
                self._orphan_p_count += 1
                if self._orphan_p_since is None:
                    self._orphan_p_since = now
                    self._blackout_start = now
                # Report first orphan and then every 10th
                if self._orphan_p_count == 1:
                    self._glitch("orphan_p",
                                 "P-frame without preceding keyframe "
                                 "(decoder will show black)")
                elif self._orphan_p_count % 25 == 0:
                    dur = now - self._orphan_p_since
                    self._glitch("orphan_p",
                                 f"{self._orphan_p_count} orphan P-frames "
                                 f"over {dur*1000:.0f}ms")

        # ---- Track transition decode health ----
        if self._active_transition is not None:
            self._transition_chunks += 1
            if has_idr:
                # First keyframe after transition — record latency
                lat = (now - self._active_transition.timestamp) * 1000
                self._active_transition.first_keyframe_latency_ms = lat
                self._active_transition.chunks_before_keyframe = self._transition_chunks - 1

                # Determine blackout risk based on transition direction:
                is_speedup = self._active_transition.to_fps > self._active_transition.from_fps

                if is_speedup:
                    # LOW→HIGH (3→25): orphan P-frames before IDR = blackout
                    if self._active_transition.orphan_p_count > 0:
                        self._active_transition.blackout_risk = True
                else:
                    # HIGH→LOW (25→3): long latency to first keyframe = blackout
                    # At 25→3, jmuxer expects data every 40ms; if gap > 500ms
                    # the decoder stalls and shows black.
                    if lat > 500:
                        self._active_transition.blackout_risk = True

                self.stats.transition_events.append(self._active_transition)
                t = now - self.stats.start_time
                direction = "↑" if is_speedup else "↓"
                risk = "⚠️  BLACKOUT" if self._active_transition.blackout_risk else "✅"
                print(f"  [{t:.1f}s] transition {direction} "
                      f"{self._active_transition.from_fps}→"
                      f"{self._active_transition.to_fps}fps: "
                      f"1st_kf={lat:.0f}ms, "
                      f"orphan_p={self._active_transition.orphan_p_count}, "
                      f"data_chunks={self._transition_chunks}  {risk}")
                self._active_transition = None
            elif has_p:
                self._active_transition.orphan_p_count += 1

        # Keyframe gap check: if too long since last keyframe, flag it
        if self._last_keyframe_time is not None and self._decoder_has_keyframe:
            kf_age = now - self._last_keyframe_time
            expected_fps = self._reported_fps or 25
            # iperiod=15, so expect keyframe every 15/fps seconds
            expected_kf_interval = 15.0 / max(expected_fps, 1)
            if kf_age > expected_kf_interval * 3 and kf_age > 2.0:
                # Only flag once per long gap
                if not hasattr(self, '_kf_gap_flagged') or \
                   now - self._kf_gap_flagged > expected_kf_interval * 2:
                    self._kf_gap_flagged = now
                    self._glitch("gap", f"no keyframe for {kf_age*1000:.0f}ms "
                                 f"(expected ~{expected_kf_interval*1000:.0f}ms)")

        # ---- Duplicate detection ----
        if md5 == self._h264_prev_chunk_hash:
            self._consecutive_dupes += 1
            self._glitch("duplicate", f"identical h264 chunk md5={md5[:12]}… "
                         f"({len(data)} bytes, nals={nal_types}, "
                         f"consecutive: {self._consecutive_dupes})")
            if self._frozen_since is None:
                self._frozen_since = now
        else:
            if self._frozen_since is not None:
                frozen_dur = now - self._frozen_since
                if frozen_dur > 2.0:
                    self._glitch("frozen", f"stream frozen for {frozen_dur:.1f}s")
                self._frozen_since = None
            if self._consecutive_dupes >= 3:
                self._glitch("repeat_burst",
                             f"{self._consecutive_dupes} consecutive "
                             f"identical h264 chunks")
            self._consecutive_dupes = 0

        # ---- Timing analysis ----
        if self._prev_time is not None:
            dt = now - self._prev_time
            self.stats.frame_intervals.append(dt)
            if len(self.stats.frame_intervals) > 10:
                expected_fps = self._reported_fps or 1
                expected_interval = 1.0 / max(expected_fps, 0.5)
                # Idle mode blackouts are visible well before 3× interval.
                if expected_fps <= 5:
                    min_gap = max(expected_interval * 2.0, 0.45)
                # After FPS transition, allow 1.2s grace for first keyframe
                # (iperiod=15 at 25fps → ~600ms keyframe interval).
                elif not had_keyframe_before:
                    min_gap = max(expected_interval * 3, 1.2)
                else:
                    min_gap = max(expected_interval * 3, 0.5)
                if dt > min_gap:
                    self._glitch("gap", f"interval {dt*1000:.0f}ms "
                                 f"(expected {expected_interval*1000:.0f}ms, "
                                 f"reported_fps={expected_fps})")

        self._prev_time = now
        self._h264_prev_chunk_hash = md5

        # ---- Start code check ----
        if len(data) > 4:
            if not (data[:4] == b'\x00\x00\x00\x01' or
                    data[:3] == b'\x00\x00\x01'):
                self._glitch("corrupt",
                             f"h264 chunk missing start code "
                             f"(first 4 bytes: {data[:4].hex()})")

        # ---- Accumulate for periodic decode check ----
        # Only start accumulating from a keyframe so ffmpeg has SPS/PPS context
        if has_idr:
            self._h264_stream_buf.clear()
        if self._decoder_has_keyframe:
            self._h264_stream_buf.extend(data)
        # Keep buffer bounded (last ~2MB)
        if len(self._h264_stream_buf) > 2 * 1024 * 1024:
            self._h264_stream_buf = self._h264_stream_buf[-1024*1024:]
        if now - self._last_decode_check >= self._decode_check_interval:
            self._run_decode_check()
            self._last_decode_check = now

    def _run_decode_check(self):
        """Run ffmpeg to validate H.264 stream decodability."""
        import subprocess
        import tempfile
        buf = bytes(self._h264_stream_buf)
        if len(buf) < 100:
            return
        try:
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "h264", "-i", "pipe:0",
                 "-f", "null", "-"],
                input=buf, capture_output=True, timeout=5,
            )
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            if stderr:
                # Count distinct error lines
                errors = [l for l in stderr.splitlines() if l.strip()]
                for err in errors[:3]:  # report first 3
                    self._glitch("decode_error", f"ffmpeg: {err[:120]}")
                if self.verbose and len(errors) > 3:
                    print(f"    ... ({len(errors)} total decode errors)")
        except FileNotFoundError:
            pass  # ffmpeg not available — skip decode check
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            if self.verbose:
                print(f"    [decode_check] error: {e}")
        # Clear buffer after check
        self._h264_stream_buf.clear()

    def _reset_decode_state(self, reason: str = ""):
        """Reset decoder state tracking (e.g. on FPS transition)."""
        if self.verbose and reason:
            t = time.monotonic() - self.stats.start_time
            print(f"  [{t:.1f}s] decode state reset: {reason}")
        self._decoder_has_keyframe = False
        self._decoder_has_sps = False
        self._decoder_has_pps = False
        self._orphan_p_count = 0
        self._orphan_p_since = None
        self._blackout_start = None

    def _handle_metadata(self, msg: dict):
        self.encoding = msg.get("encoding", self.encoding)
        snap = MetaSnapshot(
            timestamp=time.monotonic(),
            current_fps=msg.get("current_fps", 0),
            bitrate_kbps=msg.get("bitrate_kbps", 0),
            h264_bitrate=msg.get("h264_bitrate", 0),
            send_rate_mbps=msg.get("send_rate_mbps", 0),
            wifi_signal_dbm=msg.get("wifi_signal_dbm", 0),
            cpu_percent=msg.get("cpu_percent", 0),
            resolution=msg.get("resolution", ""),
            motion_pixels=msg.get("motion_pixels", 0),
            detection_active=msg.get("detection_active", False),
            keyframe=msg.get("keyframe", False),
            frame_count=msg.get("frame_count", 0),
        )
        self.stats.meta_snapshots.append(snap)

        # Track FPS transitions
        new_fps = snap.current_fps
        if self._reported_fps is not None and new_fps != self._reported_fps:
            self.stats.fps_transitions.append(
                (time.monotonic(), self._reported_fps, new_fps))
            t = time.monotonic() - self.stats.start_time
            print(f"  [{t:.1f}s] FPS transition: {self._reported_fps} → {new_fps}")

            # Finalize any in-flight transition that never got a keyframe
            if self._active_transition is not None:
                if self._active_transition.first_keyframe_latency_ms < 0:
                    self._active_transition.blackout_risk = True
                self.stats.transition_events.append(self._active_transition)
                self._active_transition = None

            # Start tracking new transition (especially idle→active)
            is_speedup = new_fps > self._reported_fps
            self._active_transition = TransitionEvent(
                timestamp=time.monotonic(),
                from_fps=self._reported_fps,
                to_fps=new_fps,
                detection_active=snap.detection_active,
            )
            self._transition_chunks = 0

            # Reset decode state — FPS change means decoder may lose sync
            self._reset_decode_state(
                f"FPS {self._reported_fps}→{new_fps}")
            # Clear decode check buffer — cross-transition data causes
            # false positive PPS errors in ffmpeg
            self._h264_stream_buf.clear()
        self._reported_fps = new_fps

        # Track motion detection state changes
        if self._last_detection_active is not None and \
           snap.detection_active != self._last_detection_active:
            t = time.monotonic() - self.stats.start_time
            state = "DETECTED" if snap.detection_active else "Idle"
            if self.verbose:
                print(f"  [{t:.1f}s] motion: {'Idle' if self._last_detection_active else 'DETECTED'} → {state}")
        self._last_detection_active = snap.detection_active

        # Print first metadata once
        if not self._meta_printed:
            self._meta_printed = True
            print(f"[meta] encoding={self.encoding}  "
                  f"fps={snap.current_fps}  "
                  f"res={snap.resolution}  "
                  f"bitrate={snap.bitrate_kbps:.0f}kbps  "
                  f"h264_target={snap.h264_bitrate/1000:.0f}kbps  "
                  f"wifi={snap.wifi_signal_dbm}dBm  "
                  f"cpu={snap.cpu_percent:.0f}%")

    def run(self):
        ws_scheme = "wss" if self.use_https else "ws"
        url = (f"{ws_scheme}://{self.host}/ws/viewer/"
               f"?camera_id={self.camera_id}"
               f"&session_key={self.sessionid}")

        print(f"[ws] Connecting to {ws_scheme}://{self.host}/ws/viewer/?camera_id={self.camera_id}")
        ws = websocket.WebSocket()
        ws.settimeout(10)

        try:
            ws.connect(
                url,
                header=[f"Cookie: sessionid={self.sessionid}"],
            )
        except Exception as e:
            sys.exit(f"WebSocket connection failed: {e}")

        print(f"[ws] Connected. Analyzing for {self.duration}s ...")
        self.stats.start_time = time.monotonic()
        deadline = self.stats.start_time + self.duration

        try:
            while time.monotonic() < deadline:
                try:
                    opcode, raw = ws.recv_data(control_frame=True)
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    print("[ws] Connection closed by server")
                    break

                if opcode == 1:
                    text = raw.decode("utf-8", errors="replace")
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type", "")

                    if msg_type == "frame":
                        self.seq += 1
                        self.stats.total_frames += 1
                        frame_data = base64.b64decode(msg["data"])
                        self.stats.total_bytes += len(frame_data)
                        self._process_jpeg_frame(frame_data)
                        self._print_progress()
                    elif "encoding" in msg:
                        self._handle_metadata(msg)

                elif opcode == 2:
                    self.seq += 1
                    self.stats.total_frames += 1
                    self.stats.total_bytes += len(raw)
                    self._process_h264_chunk(raw)
                    self._print_progress()

        except KeyboardInterrupt:
            print("\n[!] Interrupted by user")
        finally:
            ws.close()

        if self._frozen_since is not None:
            frozen_dur = time.monotonic() - self._frozen_since
            if frozen_dur > 2.0:
                self._glitch("frozen", f"stream still frozen at end ({frozen_dur:.1f}s)")

        # Finalize any in-flight transition
        if self._active_transition is not None:
            if self._active_transition.first_keyframe_latency_ms < 0:
                self._active_transition.blackout_risk = True
            self.stats.transition_events.append(self._active_transition)

        print(self.stats.report())

    def _print_progress(self):
        if self.seq % 30 == 0:
            elapsed = time.monotonic() - self.stats.start_time
            fps = self.seq / elapsed if elapsed > 0 else 0
            glitches = len(self.stats.glitches)
            rfps = self._reported_fps or "?"
            dupes = self.stats.duplicates
            print(f"  [{elapsed:.0f}s] frames={self.seq}  "
                  f"measured_fps={fps:.1f}  reported_fps={rfps}  "
                  f"dupes={dupes}  glitches={glitches}  "
                  f"enc={self.encoding}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Stream diagnostics — detect duplicate/repeated frames, "
                    "stuttering, and visual corruption in a live WebSocket stream."
    )
    parser.add_argument("--host", default="192.0.2.11",
                        help="Server hostname or IP (default: 192.0.2.11)")
    parser.add_argument("--user", "-u", required=True,
                        help="Login username")
    parser.add_argument("--password", "-p", required=True,
                        help="Login password")
    parser.add_argument("--camera", default="rpi-cam",
                        help="Camera ID (default: rpi-cam)")
    parser.add_argument("--duration", "-d", type=float, default=30,
                        help="Analysis duration in seconds (default: 30)")
    parser.add_argument("--https", action="store_true",
                        help="Use HTTPS/WSS")
    parser.add_argument("--save-frames", action="store_true",
                        help="Save duplicate/corrupt frames to disk")
    parser.add_argument("--save-dir", default="diag_frames",
                        help="Directory for saved frames (default: diag_frames)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each glitch as it happens")
    args = parser.parse_args()

    sessionid = login(args.host, args.user, args.password,
                      use_https=args.https)

    analyzer = StreamAnalyzer(
        host=args.host,
        sessionid=sessionid,
        camera_id=args.camera,
        duration=args.duration,
        use_https=args.https,
        save_frames=args.save_frames,
        save_dir=args.save_dir,
        verbose=args.verbose,
    )
    analyzer.run()


if __name__ == "__main__":
    main()
