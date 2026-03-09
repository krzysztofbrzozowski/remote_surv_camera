"""WebRTC SFU relay — receives H.264 from camera WebSocket, delivers via WebRTC."""
import asyncio
import fractions
import logging
import time

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaRelay

# Increase ICE consent tolerance — the default (6 failures × 5s = 30s)
# causes premature connection closure when behind Cloudflare or on lossy links.
import aioice.ice as _aioice
_aioice.CONSENT_FAILURES = 24  # ~2 minutes of failures before closing

# Monkey-patch: wait for DTLS handshake instead of raising ConnectionError
# immediately. Without this, _run_rtp exits silently when DTLS hasn't finished
# by the time the first frame is ready (queue already has data).
from aiortc.rtcdtlstransport import RTCDtlsTransport, State as _DtlsState

_original_dtls_send_rtp = RTCDtlsTransport._send_rtp


async def _dtls_send_rtp_with_wait(self, data: bytes) -> None:
    if self._state != _DtlsState.CONNECTED:
        for _ in range(50):  # wait up to 5s for DTLS handshake
            if self._state == _DtlsState.CONNECTED:
                break
            if self._state in (_DtlsState.CLOSED, _DtlsState.FAILED):
                break
            await asyncio.sleep(0.1)
    return await _original_dtls_send_rtp(self, data)


RTCDtlsTransport._send_rtp = _dtls_send_rtp_with_wait

logger = logging.getLogger(__name__)

VIDEO_CLOCK_RATE = 90000
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)


def _is_h264_keyframe_bytes(data: bytes) -> bool:
    """Detect H.264 keyframe (IDR/SPS NAL) from Annex B bytestream."""
    i = 0
    limit = min(len(data), 128)
    while i < limit - 4:
        if data[i:i + 4] == b'\x00\x00\x00\x01':
            if i + 4 < len(data) and (data[i + 4] & 0x1F) in (5, 7):
                return True
            i += 4
        elif data[i:i + 3] == b'\x00\x00\x01':
            if i + 3 < len(data) and (data[i + 3] & 0x1F) in (5, 7):
                return True
            i += 3
        else:
            i += 1
    return False

# ICE servers — empty by default (no external STUN dependency).
# Set STUN_SERVER env var for NAT traversal, e.g. "stun:stun.l.google.com:19302"
# Set TURN_SERVER + TURN_USERNAME + TURN_PASSWORD for relay through restrictive NATs.
import os as _os

_stun_url = _os.environ.get("STUN_SERVER", "")
_turn_url = _os.environ.get("TURN_SERVER", "")
_turn_user = _os.environ.get("TURN_USERNAME", "")
_turn_pass = _os.environ.get("TURN_PASSWORD", "")

ICE_SERVERS: list[RTCIceServer] = []
if _stun_url:
    ICE_SERVERS.append(RTCIceServer(urls=_stun_url))
if _turn_url:
    # Provide both UDP and TCP TURN — TCP works through stricter firewalls
    tcp_url = (_turn_url.replace("?transport=udp", "?transport=tcp")
               if "?transport=" in _turn_url else _turn_url + "?transport=tcp")
    ICE_SERVERS.append(RTCIceServer(
        urls=[_turn_url, tcp_url],
        username=_turn_user,
        credential=_turn_pass,
    ))


class CameraVideoTrack(MediaStreamTrack):
    """Receives H.264 from camera WebSocket, forwards as av.Packet (no decode).

    Returns av.Packet from recv() so aiortc uses its pack() path —
    H.264 NALUs are split and packetized into RTP directly, with zero
    decode/re-encode overhead (true SFU passthrough).
    """

    kind = "video"

    def __init__(self, camera_id: str):
        super().__init__()
        self.camera_id = camera_id
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=30)
        self._start: float | None = None
        self._push_count = 0
        self._recv_count = 0
        self._last_keyframe: bytes | None = None

    def push_h264(self, data: bytes):
        """Push raw Annex B H.264 access unit (called from CameraConsumer)."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(data)
            self._push_count += 1
            if self._push_count % 30 == 1:
                logger.debug(
                    "Track [%s] push #%d (queue=%d/%d, %d bytes)",
                    self.camera_id, self._push_count,
                    self._queue.qsize(), self._queue.maxsize, len(data),
                )
        except asyncio.QueueFull:
            pass

    async def recv(self) -> av.Packet:
        """Return raw H.264 as av.Packet — aiortc will packetize, not re-encode.

        If no new frame arrives within 2 seconds, re-send the last keyframe
        to keep the RTP sender alive and the browser decoder fed.
        """
        while True:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=2.0)
                # Cache keyframes for re-send during idle gaps
                if _is_h264_keyframe_bytes(data):
                    self._last_keyframe = data
                break
            except asyncio.TimeoutError:
                # No new frame — re-send last keyframe to keep stream alive
                if self._last_keyframe:
                    data = self._last_keyframe
                    break
                # No keyframe cached yet — keep waiting
                continue
        if self._start is None:
            self._start = time.time()
        pts = int((time.time() - self._start) * VIDEO_CLOCK_RATE)
        packet = av.Packet(data)
        packet.pts = pts
        packet.dts = pts  # No B-frames → DTS equals PTS
        packet.time_base = VIDEO_TIME_BASE
        self._recv_count += 1
        if self._recv_count % 30 == 1:
            logger.info(
                "Track [%s] recv #%d (pts=%d, %d bytes, queue=%d)",
                self.camera_id, self._recv_count, pts, len(data),
                self._queue.qsize(),
            )
        return packet


class CameraRelayTrack:
    """Wraps CameraVideoTrack + MediaRelay for fan-out to multiple viewers."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._source = CameraVideoTrack(camera_id)
        self._relay = MediaRelay()

    def push_h264(self, data: bytes):
        self._source.push_h264(data)

    def subscribe(self):
        """Return a relay track for one viewer's RTCPeerConnection."""
        return self._relay.subscribe(self._source)

    def stop(self):
        self._source.stop()


# ── Global state ─────────────────────────────────────────────────────
_camera_tracks: dict[str, CameraRelayTrack] = {}
_peer_connections: set[RTCPeerConnection] = set()


def get_or_create_track(camera_id: str) -> CameraRelayTrack:
    if camera_id not in _camera_tracks:
        _camera_tracks[camera_id] = CameraRelayTrack(camera_id)
        logger.info("WebRTC track created for camera [%s]", camera_id)
    return _camera_tracks[camera_id]


def remove_track(camera_id: str):
    track = _camera_tracks.pop(camera_id, None)
    if track:
        track.stop()
        logger.info("WebRTC track removed for camera [%s]", camera_id)


async def close_peer_connection(pc: RTCPeerConnection):
    """Close a specific peer connection."""
    _peer_connections.discard(pc)
    await pc.close()


async def cleanup_all():
    """Shutdown all WebRTC resources."""
    for pc in list(_peer_connections):
        await pc.close()
    _peer_connections.clear()
    for track in _camera_tracks.values():
        track.stop()
    _camera_tracks.clear()
