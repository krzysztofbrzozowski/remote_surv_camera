#!/usr/bin/env python3
"""
WebRTC Stream Diagnostics Tool
===============================
Connects to the surveillance camera server as a real WebRTC viewer (via aiortc),
receives video frames, and analyzes stream health: frame timing, freezes,
disconnects, ICE failures, decode quality, FPS transitions, and metadata.

Architecture:
    1. HTTP login → session cookie
    2. WebSocket → signaling (SDP offer/answer) + metadata
    3. WebRTC (aiortc) → video frames (decoded by aiortc)

Usage:
    python stream_diagnostics.py --host 192.168.1.100 -u admin -p admin
    python stream_diagnostics.py --host 192.168.1.100 -u admin -p admin --duration 600
    python stream_diagnostics.py --host 192.168.1.100 -u admin -p admin -v --save-frames
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

try:
    import websockets
except ImportError:
    sys.exit("Missing websockets. Install: pip install websockets")

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger("diag")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GlitchEvent:
    """A detected anomaly in the stream."""
    frame_seq: int
    timestamp: float
    kind: str       # duplicate | gap | frozen | corrupt | dimension_change | disconnect
    detail: str


@dataclass
class MetaSnapshot:
    """A snapshot of streamer metadata."""
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
    encoding: str = ""


@dataclass
class TransitionEvent:
    """Tracks an FPS transition."""
    timestamp: float
    from_fps: float
    to_fps: float
    detection_active: bool = False
    frames_after: int = 0
    latency_to_first_frame_ms: float = -1


@dataclass
class WebRTCEvent:
    """A WebRTC connection state change event."""
    timestamp: float
    kind: str
    detail: str


@dataclass
class DiagStats:
    """Accumulator for the diagnostic run."""
    start_time: float = 0.0
    total_frames: int = 0
    total_bytes_estimate: int = 0

    # Frame quality
    duplicates: int = 0
    gaps: int = 0
    frozen_periods: int = 0
    corrupt_frames: int = 0
    dimension_changes: int = 0

    # Timing
    frame_intervals: list = field(default_factory=list)
    frame_sizes_wh: list = field(default_factory=list)

    # WebRTC health
    webrtc_events: list = field(default_factory=list)
    webrtc_disconnects: int = 0
    webrtc_reconnects: int = 0
    webrtc_ice_failures: int = 0
    time_to_first_frame_ms: float = -1
    total_connected_time: float = 0
    total_disconnected_time: float = 0

    # Metadata
    fps_transitions: list = field(default_factory=list)
    transition_events: list = field(default_factory=list)
    meta_snapshots: list = field(default_factory=list)
    glitches: list = field(default_factory=list)

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
            "=" * 72,
            "  WEBRTC STREAM DIAGNOSTICS REPORT",
            "=" * 72,
            f"  Duration           : {elapsed:.1f}s",
            f"  Total frames (RTP) : {self.total_frames}",
            f"  Average FPS        : {avg_fps:.2f}",
        ]

        if self.frame_sizes_wh:
            last_wh = self.frame_sizes_wh[-1]
            unique_sizes = set(self.frame_sizes_wh)
            lines.append(f"  Frame dimensions   : {last_wh[0]}x{last_wh[1]}"
                         + (f" ({len(unique_sizes)} unique)" if len(unique_sizes) > 1 else ""))

        lines += [
            "",
            "  Frame interval (ms):",
            f"    mean={avg_dt:.1f}  std={std_dt:.1f}  min={min_dt:.1f}  max={max_dt:.1f}",
            f"    p95={p95_dt:.1f}  p99={p99_dt:.1f}  jitter={jitter:.1f}%",
        ]

        # WebRTC health
        ttf_str = (f"{self.time_to_first_frame_ms:.0f}ms"
                   if self.time_to_first_frame_ms >= 0
                   else "N/A (no frames)")
        conn_pct = (f"{self.total_connected_time / elapsed * 100:.1f}%"
                    if elapsed > 0 else "0%")
        lines += [
            "",
            "  WebRTC connection health:",
            f"    Time to first frame   : {ttf_str}",
            f"    Disconnects           : {self.webrtc_disconnects}",
            f"    Reconnects            : {self.webrtc_reconnects}",
            f"    ICE failures          : {self.webrtc_ice_failures}",
            f"    Connected time        : {self.total_connected_time:.1f}s ({conn_pct})",
            f"    Disconnected time     : {self.total_disconnected_time:.1f}s",
        ]

        if self.webrtc_events:
            lines.append("")
            lines.append("  WebRTC events (last 30):")
            for ev in self.webrtc_events[-30:]:
                t = ev.timestamp - self.start_time
                lines.append(f"    [{t:7.2f}s] {ev.kind:<20} {ev.detail}")

        # FPS transitions
        if self.fps_transitions:
            lines.append("")
            lines.append("  FPS transitions:")
            for t, old, new in self.fps_transitions[-20:]:
                rel = t - self.start_time
                lines.append(f"    [{rel:7.2f}s] {old} -> {new} fps")

        if self.transition_events:
            lines.append("")
            lines.append("  Transition frame health:")
            for te in self.transition_events:
                rel = te.timestamp - self.start_time
                lat = (f"{te.latency_to_first_frame_ms:.0f}ms"
                       if te.latency_to_first_frame_ms >= 0 else "NO FRAME")
                direction = "UP" if te.to_fps > te.from_fps else "DOWN"
                lines.append(
                    f"    [{rel:7.2f}s] {direction} {te.from_fps}->{te.to_fps}fps  "
                    f"1st_frame={lat}  frames_after={te.frames_after}  "
                    f"motion={te.detection_active}")

        # Metadata summary
        if self.meta_snapshots:
            last = self.meta_snapshots[-1]
            lines += [
                "",
                "  Last streamer metadata:",
                f"    reported fps   : {last.current_fps}",
                f"    bitrate        : {last.bitrate_kbps:.0f} kbps",
                f"    h264 target    : {last.h264_bitrate / 1000:.0f} kbps",
                f"    send rate      : {last.send_rate_mbps:.2f} Mbps",
                f"    wifi signal    : {last.wifi_signal_dbm} dBm",
                f"    cpu            : {last.cpu_percent:.0f}%",
                f"    resolution     : {last.resolution}",
                f"    encoding       : {last.encoding}",
                f"    detection      : {last.detection_active}",
                f"    motion pixels  : {last.motion_pixels}",
            ]

        # Glitch summary
        lines += [
            "",
            "  Glitch summary:",
            f"    Exact duplicates        : {self.duplicates}",
            f"    Large gaps (>3x expect) : {self.gaps}",
            f"    Frozen periods (>2s)    : {self.frozen_periods}",
            f"    Corrupt frames          : {self.corrupt_frames}",
            f"    Dimension changes       : {self.dimension_changes}",
            f"    WebRTC disconnects      : {self.webrtc_disconnects}",
            f"    TOTAL glitch events     : {len(self.glitches)}",
        ]

        if self.glitches:
            lines.append("")
            lines.append("  Glitch log (last 50):")
            for g in self.glitches[-50:]:
                t = g.timestamp - self.start_time
                lines.append(
                    f"    [{t:7.2f}s] frame#{g.frame_seq:>5}  "
                    f"{g.kind:<20} {g.detail}")

        lines.append("")
        total_issues = len(self.glitches)
        dc = self.webrtc_disconnects
        if total_issues == 0 and dc == 0:
            verdict = "PASS - STREAM LOOKS CLEAN"
        elif dc > 2:
            verdict = (f"FAIL - UNSTABLE CONNECTION: {dc} WebRTC "
                       f"disconnects during {elapsed:.0f}s test")
        elif total_issues < 5 and dc <= 1:
            verdict = "WARN - MINOR GLITCHES DETECTED"
        elif total_issues < 20:
            verdict = "WARN - MODERATE GLITCH LEVEL"
        else:
            verdict = "FAIL - SIGNIFICANT GLITCHES"

        if self.total_frames == 0:
            verdict = "FAIL - NO FRAMES RECEIVED"

        lines.append(f"  Verdict: {verdict}")
        lines.append("=" * 72)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def http_login(host: str, username: str, password: str,
               use_https: bool = False) -> str:
    """Login via HTTP and return session ID."""
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
# Frame analysis helpers
# ---------------------------------------------------------------------------

def frame_hash(frame_array: np.ndarray) -> str:
    """Fast hash of downsampled frame for duplicate detection."""
    h, w = frame_array.shape[:2]
    small = frame_array[::max(1, h // 90), ::max(1, w // 160)]
    return hashlib.md5(small.tobytes()).hexdigest()


def structural_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Simplified SSIM-like metric (0..1). 1 = identical."""
    if a.shape != b.shape:
        return 0.0
    h, w = a.shape[:2]
    step_h, step_w = max(1, h // 90), max(1, w // 160)
    a = a[::step_h, ::step_w].astype(np.float64)
    b = b[::step_h, ::step_w].astype(np.float64)
    mu_a, mu_b = a.mean(), b.mean()
    sig_a, sig_b = a.std(), b.std()
    sig_ab = ((a - mu_a) * (b - mu_b)).mean()
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    return float(
        ((2 * mu_a * mu_b + C1) * (2 * sig_ab + C2)) /
        ((mu_a ** 2 + mu_b ** 2 + C1) * (sig_a ** 2 + sig_b ** 2 + C2))
    )


# ---------------------------------------------------------------------------
# WebRTC Stream Analyzer
# ---------------------------------------------------------------------------

class WebRTCStreamAnalyzer:
    """Connects as a WebRTC viewer, receives and analyzes video frames."""

    def __init__(self, host, sessionid, camera_id="rpi-cam",
                 duration=30.0, use_https=False, save_frames=False,
                 save_dir="diag_frames", verbose=False):
        self.host = host
        self.sessionid = sessionid
        self.camera_id = camera_id
        self.duration = duration
        self.use_https = use_https
        self.save_frames = save_frames
        self.save_dir = save_dir
        self.verbose = verbose

        self.stats = DiagStats()
        self.seq = 0
        self._reported_fps: Optional[float] = None
        self._meta_printed = False

        # Frame state
        self._recent_hashes: deque = deque(maxlen=120)
        self._prev_array: Optional[np.ndarray] = None
        self._prev_time: Optional[float] = None
        self._prev_dims: Optional[tuple] = None
        self._consecutive_dupes = 0
        self._frozen_since: Optional[float] = None

        # WebRTC state
        self._pc: Optional[RTCPeerConnection] = None
        self._ws = None
        self._connected = False
        self._connected_since: Optional[float] = None
        self._disconnected_since: Optional[float] = None
        self._first_frame_time: Optional[float] = None
        self._ws_connect_time: Optional[float] = None

        # Transition tracking
        self._active_transition: Optional[TransitionEvent] = None
        self._last_detection_active: Optional[bool] = None

        self._running = True

        if save_frames:
            os.makedirs(save_dir, exist_ok=True)

    def _glitch(self, kind, detail):
        ev = GlitchEvent(self.seq, time.monotonic(), kind, detail)
        self.stats.glitches.append(ev)
        for attr, k in [("duplicates", "duplicate"), ("gaps", "gap"),
                        ("frozen_periods", "frozen"), ("corrupt_frames", "corrupt"),
                        ("dimension_changes", "dimension_change")]:
            if kind == k:
                setattr(self.stats, attr, getattr(self.stats, attr) + 1)
        if self.verbose:
            t = ev.timestamp - self.stats.start_time
            print(f"  !! [{t:.2f}s] #{self.seq} {kind}: {detail}")

    def _webrtc_event(self, kind, detail):
        ev = WebRTCEvent(time.monotonic(), kind, detail)
        self.stats.webrtc_events.append(ev)
        if self.verbose:
            t = ev.timestamp - self.stats.start_time
            print(f"  >> [{t:.2f}s] {kind}: {detail}")

    def _process_video_frame(self, frame):
        """Process a decoded VideoFrame from aiortc."""
        now = time.monotonic()
        self.seq += 1
        self.stats.total_frames += 1

        if self._first_frame_time is None:
            self._first_frame_time = now
            if self._ws_connect_time:
                ttf = (now - self._ws_connect_time) * 1000
                self.stats.time_to_first_frame_ms = ttf
                print(f"  [WebRTC] First frame: {ttf:.0f}ms after WS connect")

        try:
            img = frame.to_ndarray(format="rgb24")
        except Exception as e:
            self._glitch("corrupt", f"to_ndarray failed: {e}")
            return

        h, w = img.shape[:2]
        self.stats.frame_sizes_wh.append((w, h))
        self.stats.total_bytes_estimate += w * h * 3

        # Dimension change
        if self._prev_dims and (w, h) != self._prev_dims:
            self._glitch("dimension_change",
                         f"{self._prev_dims[0]}x{self._prev_dims[1]} -> {w}x{h}")
        self._prev_dims = (w, h)

        # Duplicate detection
        fhash = frame_hash(img)
        is_dupe = fhash in self._recent_hashes

        # Near-duplicate check (only for frozen detection, not per-frame glitching)
        ssim_val = 1.0
        if self._prev_array is not None:
            try:
                ssim_val = structural_similarity(img, self._prev_array)
            except Exception:
                ssim_val = 0.0

        # Exact hash dup = same encoded frame re-sent (real bug)
        if is_dupe:
            self._consecutive_dupes += 1
            self._glitch("duplicate",
                         f"exact dup hash={fhash[:12]} "
                         f"(consecutive: {self._consecutive_dupes})")
            if self._frozen_since is None:
                self._frozen_since = now
        elif ssim_val > 0.9999:
            # Very high similarity — track for frozen detection only
            self._consecutive_dupes += 1
            if self._frozen_since is None:
                self._frozen_since = now
        else:
            if self._frozen_since:
                dur = now - self._frozen_since
                if dur > 2.0:
                    self._glitch("frozen",
                                 f"frozen for {dur:.1f}s "
                                 f"({self._consecutive_dupes} near-identical frames)")
                self._frozen_since = None
            self._consecutive_dupes = 0

        # Timing
        if self._prev_time is not None:
            dt = now - self._prev_time
            self.stats.frame_intervals.append(dt)
            if len(self.stats.frame_intervals) > 10:
                exp_fps = self._reported_fps or 1
                exp_interval = 1.0 / max(exp_fps, 0.5)
                min_gap = max(exp_interval * 3, 0.5)
                if dt > min_gap:
                    self._glitch("gap",
                                 f"interval {dt * 1000:.0f}ms "
                                 f"(expected {exp_interval * 1000:.0f}ms)")

        # Update transition tracking
        if self._active_transition is not None:
            self._active_transition.frames_after += 1
            if self._active_transition.latency_to_first_frame_ms < 0:
                lat = (now - self._active_transition.timestamp) * 1000
                self._active_transition.latency_to_first_frame_ms = lat

        self._prev_time = now
        self._prev_array = img
        self._recent_hashes.append(fhash)

        if self.save_frames and is_dupe and Image:
            path = os.path.join(self.save_dir, f"dup_{self.seq:06d}.png")
            Image.fromarray(img).save(path)

    def _handle_metadata(self, msg):
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
            encoding=msg.get("encoding", ""),
        )
        self.stats.meta_snapshots.append(snap)

        new_fps = snap.current_fps
        if self._reported_fps is not None and new_fps != self._reported_fps:
            self.stats.fps_transitions.append(
                (time.monotonic(), self._reported_fps, new_fps))
            t = time.monotonic() - self.stats.start_time
            print(f"  [{t:.1f}s] FPS: {self._reported_fps} -> {new_fps}")

            if self._active_transition is not None:
                self.stats.transition_events.append(self._active_transition)
            self._active_transition = TransitionEvent(
                timestamp=time.monotonic(),
                from_fps=self._reported_fps,
                to_fps=new_fps,
                detection_active=snap.detection_active,
            )
        self._reported_fps = new_fps

        if self._last_detection_active is not None and \
           snap.detection_active != self._last_detection_active:
            t = time.monotonic() - self.stats.start_time
            state = "DETECTED" if snap.detection_active else "Idle"
            if self.verbose:
                print(f"  [{t:.1f}s] motion: {state}")
        self._last_detection_active = snap.detection_active

        if not self._meta_printed:
            self._meta_printed = True
            print(f"[meta] enc={snap.encoding}  fps={snap.current_fps}  "
                  f"res={snap.resolution}  bitrate={snap.bitrate_kbps:.0f}kbps  "
                  f"wifi={snap.wifi_signal_dbm}dBm  cpu={snap.cpu_percent:.0f}%")

    async def _negotiate_webrtc(self):
        """Create PeerConnection, exchange SDP via WebSocket."""
        if self._pc:
            await self._pc.close()
            self._pc = None

        pc = RTCPeerConnection(
            configuration=RTCConfiguration(
                iceServers=[]
            )
        )
        self._pc = pc

        @pc.on("connectionstatechange")
        async def _on_state():
            state = pc.connectionState
            self._webrtc_event("connection_state", state)
            if state == "connected":
                self._connected = True
                self._connected_since = time.monotonic()
                if self._disconnected_since:
                    self.stats.total_disconnected_time += \
                        time.monotonic() - self._disconnected_since
                    self._disconnected_since = None
            elif state in ("disconnected", "failed", "closed"):
                if self._connected and self._running:
                    self._connected = False
                    self.stats.webrtc_disconnects += 1
                    dur = time.monotonic() - (self._connected_since or time.monotonic())
                    self._glitch("disconnect",
                                 f"WebRTC {state} after {dur:.1f}s connected")
                    if self._connected_since:
                        self.stats.total_connected_time += \
                            time.monotonic() - self._connected_since
                        self._connected_since = None
                    self._disconnected_since = time.monotonic()

        @pc.on("iceconnectionstatechange")
        async def _on_ice():
            self._webrtc_event("ice_state", pc.iceConnectionState)
            if pc.iceConnectionState == "failed":
                self.stats.webrtc_ice_failures += 1

        pc.addTransceiver("video", direction="recvonly")

        @pc.on("track")
        def _on_track(track):
            self._webrtc_event("track_received", f"kind={track.kind}")
            if track.kind == "video":
                asyncio.ensure_future(self._recv_frames(track))

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering
        if pc.iceGatheringState != "complete":
            ice_done = asyncio.Event()

            @pc.on("icegatheringstatechange")
            def _check():
                if pc.iceGatheringState == "complete":
                    ice_done.set()

            try:
                await asyncio.wait_for(ice_done.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("ICE gathering timeout")

        await self._ws.send(json.dumps({
            "type": "webrtc_offer",
            "sdp": pc.localDescription.sdp,
        }))
        self._webrtc_event("offer_sent", "SDP offer sent")

    async def _recv_frames(self, track):
        while self._running:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5.0)
                self._process_video_frame(frame)
            except asyncio.TimeoutError:
                if self._running:
                    t = time.monotonic() - self.stats.start_time
                    self._glitch("gap", f"no frame for 5s at [{t:.1f}s]")
            except Exception as e:
                if self._running and "MediaStreamError" not in type(e).__name__:
                    self._glitch("corrupt", f"recv error: {e}")
                break

    async def _ws_listener(self):
        try:
            async for message in self._ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(message)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type", "")
                if mtype == "webrtc_answer" and self._pc:
                    answer = RTCSessionDescription(sdp=msg["sdp"], type="answer")
                    await self._pc.setRemoteDescription(answer)
                    self._webrtc_event("answer_received", "SDP answer applied")
                elif mtype == "webrtc_error":
                    self._webrtc_event("server_error", msg.get("error", "?"))
                elif "encoding" in msg or "bitrate_kbps" in msg:
                    self._handle_metadata(msg)
        except websockets.ConnectionClosed as e:
            if self._running:
                self._webrtc_event("ws_closed", f"code={e.code}")
        except Exception as e:
            if self._running:
                self._webrtc_event("ws_error", str(e))

    async def _progress_printer(self):
        while self._running:
            await asyncio.sleep(10)
            if not self._running:
                break
            elapsed = time.monotonic() - self.stats.start_time
            fps = self.seq / elapsed if elapsed > 0 else 0
            gl = len(self.stats.glitches)
            rfps = self._reported_fps or "?"
            conn = "connected" if self._connected else "DISCONNECTED"
            dc = self.stats.webrtc_disconnects
            print(f"  [{elapsed:.0f}s] frames={self.seq}  fps={fps:.1f}  "
                  f"reported={rfps}  glitches={gl}  disconnects={dc}  "
                  f"webrtc={conn}")

    async def _reconnect_loop(self):
        while self._running:
            await asyncio.sleep(5)
            if not self._running:
                break
            if self._pc and self._pc.connectionState in ("failed", "closed"):
                t = time.monotonic() - self.stats.start_time
                print(f"  [{t:.1f}s] WebRTC lost -- renegotiating...")
                self.stats.webrtc_reconnects += 1
                self._webrtc_event("reconnect", "Re-negotiating WebRTC")
                try:
                    await self._negotiate_webrtc()
                except Exception as e:
                    self._webrtc_event("reconnect_failed", str(e))

    async def run(self):
        ws_scheme = "wss" if self.use_https else "ws"
        ws_url = (f"{ws_scheme}://{self.host}/ws/viewer/"
                  f"?camera_id={self.camera_id}"
                  f"&session_key={self.sessionid}")

        print(f"[ws] Connecting to {ws_scheme}://{self.host}/ws/viewer/"
              f"?camera_id={self.camera_id}")

        self.stats.start_time = time.monotonic()
        self._disconnected_since = time.monotonic()

        try:
            async with websockets.connect(
                ws_url,
                additional_headers={"Cookie": f"sessionid={self.sessionid}"},
                ping_interval=20,
                ping_timeout=30,
                close_timeout=5,
            ) as ws:
                self._ws = ws
                self._ws_connect_time = time.monotonic()
                print("[ws] Connected. Starting WebRTC negotiation...")

                await self._negotiate_webrtc()
                print(f"[diag] Analyzing for {self.duration}s ...")

                deadline = self.stats.start_time + self.duration
                tasks = [
                    asyncio.create_task(self._ws_listener()),
                    asyncio.create_task(self._progress_printer()),
                    asyncio.create_task(self._reconnect_loop()),
                ]

                try:
                    while time.monotonic() < deadline and self._running:
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass

                self._running = False
                for task in tasks:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

        except websockets.ConnectionClosed as e:
            print(f"[ws] Connection closed: code={e.code}")
        except Exception as e:
            print(f"[ws] Connection failed: {e}")

        if self._pc:
            await self._pc.close()

        # Finalize timing
        if self._connected_since:
            self.stats.total_connected_time += \
                time.monotonic() - self._connected_since
        if self._disconnected_since:
            self.stats.total_disconnected_time += \
                time.monotonic() - self._disconnected_since

        if self._frozen_since:
            dur = time.monotonic() - self._frozen_since
            if dur > 2.0:
                self._glitch("frozen", f"still frozen at end ({dur:.1f}s)")

        if self._active_transition is not None:
            self.stats.transition_events.append(self._active_transition)

        print(self.stats.report())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="WebRTC stream diagnostics -- analyze video quality, "
                    "connection stability, and frame timing via aiortc."
    )
    parser.add_argument("--host", default="192.168.1.100",
                        help="Server hostname or IP (default: 192.168.1.100)")
    parser.add_argument("--user", "-u", required=True, help="Login username")
    parser.add_argument("--password", "-p", required=True, help="Login password")
    parser.add_argument("--camera", default="rpi-cam",
                        help="Camera ID (default: rpi-cam)")
    parser.add_argument("--duration", "-d", type=float, default=30,
                        help="Analysis duration in seconds (default: 30)")
    parser.add_argument("--https", action="store_true", help="Use HTTPS/WSS")
    parser.add_argument("--save-frames", action="store_true",
                        help="Save duplicate/corrupt frames to disk")
    parser.add_argument("--save-dir", default="diag_frames",
                        help="Directory for saved frames")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each event as it happens")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aioice").setLevel(logging.WARNING)
    logging.getLogger("aiortc").setLevel(logging.WARNING)

    sessionid = http_login(args.host, args.user, args.password,
                           use_https=args.https)

    analyzer = WebRTCStreamAnalyzer(
        host=args.host,
        sessionid=sessionid,
        camera_id=args.camera,
        duration=args.duration,
        use_https=args.https,
        save_frames=args.save_frames,
        save_dir=args.save_dir,
        verbose=args.verbose,
    )

    loop = asyncio.new_event_loop()

    def _sigint():
        print("\n[!] Interrupted")
        analyzer._running = False

    loop.add_signal_handler(signal.SIGINT, _sigint)

    try:
        loop.run_until_complete(analyzer.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
