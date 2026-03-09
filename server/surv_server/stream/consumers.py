"""WebSocket consumers for camera feed and viewer."""
import asyncio
import base64
import json
import logging
import re
import time as _time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils.timezone import localtime
from django.conf import settings

logger = logging.getLogger(__name__)


def _valid_camera_id(camera_id: str) -> bool:
    """Validate camera_id against allowed pattern."""
    return bool(settings.CAMERA_ID_PATTERN.match(camera_id))


def camera_group(camera_id: str) -> str:
    return f"camera_{camera_id}"


def _is_h264_keyframe(data: bytes) -> bool:
    """Detect H.264 keyframe (IDR/SPS NAL unit) from Annex B bytestream."""
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


# Cache latest keyframe per camera for instant viewer startup
_camera_keyframes = {}  # camera_id -> bytes (SPS+PPS+IDR)
_camera_last_metadata = {}  # camera_id -> str (JSON metadata including encoding)
_camera_profile_level_id = {}  # camera_id -> str e.g. "640028" from SPS
_active_cameras = {}  # camera_id -> CameraConsumer (evict old on duplicate)

# Per-camera 24H rolling stats tracked on server side
_camera_24h_bytes = {}   # camera_id -> deque of (timestamp, bytes_delta)
_camera_24h_frames = {}  # camera_id -> deque of (timestamp, frame_delta)
# Cache for _inject_24h_stats — recalculate at most once per second
_stats_cache = {}  # camera_id -> {"t": float, "data": dict}


def _extract_profile_level_id(data: bytes) -> str | None:
    """Extract profile-level-id from H.264 Annex B SPS NAL unit."""
    i = 0
    while i < len(data) - 7:
        if data[i:i + 4] == b'\x00\x00\x00\x01' and (data[i + 4] & 0x1F) == 7:
            return f"{data[i+5]:02x}{data[i+6]:02x}{data[i+7]:02x}"
        i += 1
    return None


class CameraConsumer(AsyncWebsocketConsumer):
    """Receives frames from RPi camera and broadcasts to viewers."""

    async def connect(self):
        if not self.scope.get("camera_authenticated"):
            logger.warning("Unauthorized camera connection attempt")
            await self.accept()
            await self.close(code=4403)
            return

        query = parse_qs(self.scope.get("query_string", b"").decode())
        self.camera_id = query.get("camera_id", ["unknown"])[0]

        if not _valid_camera_id(self.camera_id):
            logger.warning(f"Invalid camera_id rejected: {self.camera_id!r}")
            await self.accept()
            await self.close(code=4400)
            return

        self.group_name = camera_group(self.camera_id)

        await self.accept()
        await self.channel_layer.group_add(self.group_name, self.channel_name)

        # Evict previous connection for the same camera_id (duplicate sender)
        old = _active_cameras.get(self.camera_id)
        if old is not None and old is not self:
            logger.warning("Evicting duplicate camera connection for [%s]", self.camera_id)
            try:
                await old.close(code=4409)
            except Exception:
                pass
        _active_cameras[self.camera_id] = self

        # Periodically refresh group membership (Redis TTL = group_expiry)
        self._group_refresh_task = asyncio.ensure_future(self._refresh_group_membership())

        await self._register_camera(self.camera_id)
        logger.info(f"Camera [{self.camera_id}] connected")

        self._encoding = None  # Set from first metadata message
        self._recording_file = None
        self._recording_size = 0
        self._recording_frames = 0
        self._recording_name = None
        self._last_metadata = {}

    async def _refresh_group_membership(self):
        """Re-add to channel group every 60s to prevent Redis TTL expiry."""
        try:
            while True:
                await asyncio.sleep(60)
                if hasattr(self, 'group_name'):
                    await self.channel_layer.group_add(self.group_name, self.channel_name)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def disconnect(self, close_code):
        task = getattr(self, '_group_refresh_task', None)
        if task:
            task.cancel()
        cid = getattr(self, 'camera_id', None)
        # Only clean up shared state if WE are the active connection
        if cid and _active_cameras.get(cid) is self:
            _active_cameras.pop(cid, None)
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
            await self._close_recording()
            await self._set_camera_offline(self.camera_id)
            _camera_keyframes.pop(cid, None)
            _camera_last_metadata.pop(cid, None)
            _camera_profile_level_id.pop(cid, None)
            _stats_cache.pop(cid, None)
            # Only remove WebRTC track if we are the active connection
            if not _active_cameras.get(cid):
                from .webrtc import remove_track
                remove_track(cid or '')
        logger.info(f"Camera [{cid or '?'}] disconnected (code={close_code})")

    async def receive(self, text_data=None, bytes_data=None):
        if not hasattr(self, '_encoding'):
            return
        if text_data:
            try:
                msg = json.loads(text_data)
                self._last_metadata = msg
                # Track encoding type from streamer metadata
                if "encoding" in msg:
                    new_enc = msg["encoding"]
                    if new_enc != self._encoding:
                        self._encoding = new_enc
                        logger.info(f"Camera [{self.camera_id}] encoding: {self._encoding}")
                        await self._start_new_recording()
                # Sync display_rotation from streamer metadata to Camera model
                if "display_rotation" in msg:
                    await self._update_camera_rotation(self.camera_id, msg["display_rotation"])

                # Inject server-side 24H rolling stats into metadata
                self._inject_24h_stats(msg)

                await self.channel_layer.group_send(self.group_name, {
                    "type": "camera.metadata",
                    "data": json.dumps(msg),
                })
                _camera_last_metadata[self.camera_id] = json.dumps(msg)
            except json.JSONDecodeError:
                pass

        elif bytes_data:
            if self._encoding is None:
                self._encoding = "jpeg"
                await self._start_new_recording()

            # Track 24H rolling bytes and frames
            now = _time.time()
            cid = self.camera_id
            if cid not in _camera_24h_bytes:
                _camera_24h_bytes[cid] = deque()
                _camera_24h_frames[cid] = deque()
            _camera_24h_bytes[cid].append((now, len(bytes_data)))
            _camera_24h_frames[cid].append((now, 1))

            # Cache keyframes (SPS+PPS+IDR) for instant viewer startup
            # Detect from binary data so caching works even when metadata
            # is sent less frequently than frames.
            if self._encoding == "h264" and _is_h264_keyframe(bytes_data):
                _camera_keyframes[self.camera_id] = bytes_data
                plid = _extract_profile_level_id(bytes_data)
                if plid:
                    _camera_profile_level_id[self.camera_id] = plid
            # Push H.264 to WebRTC relay track (viewers get video via WebRTC)
            if self._encoding == "h264":
                from .webrtc import get_or_create_track
                track = get_or_create_track(self.camera_id)
                track.push_h264(bytes_data)
            # Broadcast via channel layer only for JPEG (H.264 goes via WebRTC)
            if self._encoding != "h264":
                await self.channel_layer.group_send(self.group_name, {
                    "type": "camera.frame",
                    "data": bytes_data,
                    "encoding": self._encoding,
                })
            await self._write_to_recording(bytes_data)

    def _inject_24h_stats(self, msg: dict):
        """Replace frame_count / total_mb_sent with server-side 24H rolling values.

        Also injects ``bytes_hourly_12`` and ``frames_hourly_12`` — arrays of
        12 hourly buckets (newest last) used by the live-view bar charts.
        Buckets are aligned to clock hours so labels match wall-clock time.

        Results are cached per camera and recalculated at most once per second
        to avoid O(n) deque iteration on every metadata message.
        """
        cid = self.camera_id
        now = _time.time()

        cached = _stats_cache.get(cid)
        if cached and now - cached["t"] < 1.0:
            msg.update(cached["data"])
            return

        cutoff = now - 86400  # 24 hours

        bytes_q = _camera_24h_bytes.get(cid)
        frames_q = _camera_24h_frames.get(cid)

        # Align to clock hours: bucket 11 = current hour, bucket 0 = 11 hours ago
        from datetime import datetime as _dt
        local_now = _dt.fromtimestamp(now)
        current_hour_start = local_now.replace(minute=0, second=0, microsecond=0).timestamp()
        oldest_hour_start = current_hour_start - 11 * 3600

        bytes_buckets = [0] * 12
        frames_buckets = [0] * 12
        result = {}

        if bytes_q:
            while bytes_q and bytes_q[0][0] < cutoff:
                bytes_q.popleft()
            total_bytes_12h = 0
            for ts, b in bytes_q:
                if ts >= oldest_hour_start:
                    idx = min(int((ts - oldest_hour_start) / 3600), 11)
                    bytes_buckets[idx] += b
                    total_bytes_12h += b
            result["total_mb_sent"] = round(total_bytes_12h / (1024 * 1024), 2)

        if frames_q:
            while frames_q and frames_q[0][0] < cutoff:
                frames_q.popleft()
            total_frames_12h = 0
            for ts, f in frames_q:
                if ts >= oldest_hour_start:
                    idx = min(int((ts - oldest_hour_start) / 3600), 11)
                    frames_buckets[idx] += f
                    total_frames_12h += f
            result["frame_count"] = total_frames_12h

        # Hourly labels — aligned to clock hours
        hour_labels = []
        for i in range(12):
            h = _dt.fromtimestamp(oldest_hour_start + i * 3600).strftime("%H")
            hour_labels.append(h)

        result["bytes_hourly_12"] = [round(b / (1024 * 1024), 2) for b in bytes_buckets]
        result["frames_hourly_12"] = frames_buckets
        result["hourly_labels_12"] = hour_labels

        _stats_cache[cid] = {"t": now, "data": result}
        msg.update(result)

    async def _start_new_recording(self):
        await self._close_recording()
        ts = localtime().strftime("%Y%m%d_%H%M%S")
        ext = ".h264" if self._encoding == "h264" else ".mjpeg"
        self._recording_name = f"{self.camera_id}_rec_{ts}{ext}"
        rec_dir = Path(settings.RECORDINGS_DIR) / self.camera_id
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_path = rec_dir / self._recording_name
        self._recording_file = open(rec_path, "wb")
        self._recording_size = 0
        self._recording_frames = 0

        await self._create_recording_db(self.camera_id, self._recording_name)
        logger.info(f"Started recording: {self._recording_name}")

    async def _write_to_recording(self, frame_data: bytes):
        if not self._recording_file:
            return
        loop = asyncio.get_event_loop()
        if self._encoding == "h264":
            # Raw Annex B H.264 — directly playable by ffplay / VLC
            await loop.run_in_executor(None, self._recording_file.write, frame_data)
            self._recording_size += len(frame_data)
        else:
            # MJPEG — length-prefixed JPEG frames
            header = len(frame_data).to_bytes(4, "big")
            await loop.run_in_executor(
                None, lambda: self._recording_file.write(header + frame_data))
            self._recording_size += len(frame_data) + 4
        self._recording_frames += 1

        chunk_limit = settings.RECORDING_CHUNK_SIZE_MB * 1024 * 1024
        if self._recording_size >= chunk_limit:
            await self._update_recording_db(
                self._recording_name, self._recording_size, self._recording_frames
            )
            await self._start_new_recording()

    async def _close_recording(self):
        if self._recording_file:
            self._recording_file.close()
            if self._recording_name:
                await self._update_recording_db(
                    self._recording_name, self._recording_size, self._recording_frames
                )
            self._recording_file = None
            logger.info(f"Closed recording: {self._recording_name}")
            # Prune old recordings if camera exceeds storage quota
            await self._prune_old_recordings(self.camera_id)

    @database_sync_to_async
    def _register_camera(self, camera_id):
        from .models import Camera
        cam, _ = Camera.objects.get_or_create(camera_id=camera_id)
        cam.is_online = True
        cam.last_seen = datetime.now(timezone.utc)
        if not cam.name:
            cam.name = camera_id
        cam.save()

    @database_sync_to_async
    def _set_camera_offline(self, camera_id):
        from .models import Camera
        Camera.objects.filter(camera_id=camera_id).update(is_online=False)

    @database_sync_to_async
    def _update_camera_rotation(self, camera_id, rotation):
        from .models import Camera
        Camera.objects.filter(camera_id=camera_id).exclude(display_rotation=rotation).update(display_rotation=rotation)

    @database_sync_to_async
    def _create_recording_db(self, camera_id, filename):
        from .models import Camera, Recording
        cam = Camera.objects.get(camera_id=camera_id)
        Recording.objects.create(camera=cam, filename=filename)

    @database_sync_to_async
    def _update_recording_db(self, filename, size, frames):
        from .models import Recording
        Recording.objects.filter(filename=filename).update(
            size_bytes=size,
            frame_count=frames,
            ended_at=datetime.now(timezone.utc),
        )

    @database_sync_to_async
    def _prune_old_recordings(self, camera_id):
        from .models import Camera
        try:
            camera = Camera.objects.get(camera_id=camera_id)
        except Camera.DoesNotExist:
            return
        max_bytes = int(camera.max_storage_gb * 1024 ** 3)
        used = camera.storage_used_bytes
        if used <= max_bytes:
            return
        for rec in camera.recordings.order_by("started_at"):
            if used <= max_bytes:
                break
            rec_path = Path(settings.RECORDINGS_DIR) / camera_id / rec.filename
            mp4_path = rec_path.with_suffix(".mp4")
            for p in (rec_path, mp4_path):
                if p.exists():
                    p.unlink()
            used -= rec.size_bytes
            rec.delete()
            logger.info(f"Pruned recording {rec.filename} (quota: {camera.max_storage_gb} GB)")

    async def camera_frame(self, event):
        pass

    async def camera_metadata(self, event):
        pass


class ViewerConsumer(AsyncWebsocketConsumer):
    """Sends live camera feed to authenticated web viewers.

    Video delivery: WebRTC (H.264) via signaling over this WebSocket.
    Metadata delivery: JSON text messages over this WebSocket.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not hasattr(user, "is_authenticated") or not user.is_authenticated:
            logger.warning("Unauthenticated viewer connection rejected")
            await self.accept()
            await self.send(text_data=json.dumps({"error": "unauthenticated", "code": 4401}))
            # Small delay so browser processes the handshake before close frame
            await asyncio.sleep(0.1)
            await self.close(code=4401)
            return

        query = parse_qs(self.scope.get("query_string", b"").decode())
        self.camera_id = query.get("camera_id", ["cam-1"])[0]

        if not _valid_camera_id(self.camera_id):
            logger.warning(f"Viewer: invalid camera_id rejected: {self.camera_id!r}")
            await self.accept()
            await self.close(code=4400)
            return

        # Check user has access to this camera
        if not await self._user_can_access_camera(user, self.camera_id):
            logger.warning(f"Viewer [{user}]: access denied to camera [{self.camera_id}]")
            await self.accept()
            await self.send(text_data=json.dumps({"error": "forbidden", "code": 4403}))
            await asyncio.sleep(0.1)
            await self.close(code=4403)
            return

        self.group_name = camera_group(self.camera_id)
        self._pc = None  # RTCPeerConnection for this viewer

        await self.accept()
        await self.channel_layer.group_add(self.group_name, self.channel_name)

        # Periodically refresh group membership (Redis TTL = group_expiry)
        self._group_refresh_task = asyncio.ensure_future(self._refresh_group_membership())

        # Send cached metadata for instant stats display
        cached_meta = _camera_last_metadata.get(self.camera_id)
        if cached_meta:
            await self.send(text_data=cached_meta)

        logger.info(f"Viewer connected to [{self.camera_id}]")

    async def _refresh_group_membership(self):
        """Re-add to channel group every 60s to prevent Redis TTL expiry."""
        try:
            while True:
                await asyncio.sleep(60)
                if hasattr(self, 'group_name'):
                    await self.channel_layer.group_add(self.group_name, self.channel_name)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def disconnect(self, close_code):
        task = getattr(self, '_group_refresh_task', None)
        if task:
            task.cancel()
        if hasattr(self, 'group_name'):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)
        # Close WebRTC peer connection
        if getattr(self, '_pc', None):
            from .webrtc import close_peer_connection
            await close_peer_connection(self._pc)
            self._pc = None
        logger.info(f"Viewer disconnected from [{getattr(self, 'camera_id', '?')}] (code={close_code})")

    async def receive(self, text_data=None, bytes_data=None):
        """Handle signaling and camera switch requests."""
        if text_data:
            try:
                msg = json.loads(text_data)

                # WebRTC signaling: browser sends SDP offer
                if msg.get("type") == "webrtc_offer":
                    await self._handle_webrtc_offer(msg.get("sdp", ""))
                    return

                if msg.get("type") == "switch_camera":
                    new_id = msg["camera_id"]
                    if not _valid_camera_id(new_id):
                        logger.warning(f"Viewer: invalid switch camera_id: {new_id!r}")
                        return
                    # Check access to new camera
                    user = self.scope.get("user")
                    if not await self._user_can_access_camera(user, new_id):
                        logger.warning(f"Viewer [{user}]: access denied switching to [{new_id}]")
                        return
                    await self.channel_layer.group_discard(self.group_name, self.channel_name)
                    self.camera_id = new_id
                    self.group_name = camera_group(new_id)
                    await self.channel_layer.group_add(self.group_name, self.channel_name)

                    # Close old WebRTC connection — browser will send new offer
                    if self._pc:
                        from .webrtc import close_peer_connection
                        await close_peer_connection(self._pc)
                        self._pc = None

                    await asyncio.sleep(0)

                    await self.send(text_data=json.dumps({
                        "type": "switched",
                        "camera_id": new_id,
                    }))

                    # Send cached metadata for new camera
                    cached_meta = _camera_last_metadata.get(new_id)
                    if cached_meta:
                        await self.send(text_data=cached_meta)

                    logger.info(f"Viewer switched to [{new_id}]")
            except (json.JSONDecodeError, KeyError):
                pass

    async def _handle_webrtc_offer(self, offer_sdp: str):
        """Process SDP offer from browser, return SDP answer."""
        if not offer_sdp:
            return
        try:
            # Close existing connection if any
            if getattr(self, '_pc', None):
                from .webrtc import close_peer_connection
                await close_peer_connection(self._pc)

            from .webrtc import _peer_connections
            from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
            from .webrtc import ICE_SERVERS, get_or_create_track

            pc = RTCPeerConnection(
                configuration=RTCConfiguration(iceServers=ICE_SERVERS)
            )
            _peer_connections.add(pc)
            self._pc = pc

            @pc.on("connectionstatechange")
            async def _on_state():
                logger.info("WebRTC PC state: %s (viewer for %s)", pc.connectionState, self.camera_id)
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    _peer_connections.discard(pc)

            track = get_or_create_track(self.camera_id)
            relay_track = track.subscribe()
            pc.addTrack(relay_track)

            # Force H.264 codec (passthrough — no decode/re-encode)
            from aiortc import RTCRtpSender
            h264_codecs = [
                c for c in RTCRtpSender.getCapabilities("video").codecs
                if "h264" in c.mimeType.lower()
            ]
            for transceiver in pc.getTransceivers():
                if transceiver.kind == "video" and h264_codecs:
                    transceiver.setCodecPreferences(h264_codecs)

            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            # Patch SDP: aiortc advertises Baseline (42001f) but camera may send
            # a different profile (e.g. High). Replace profile-level-id so the
            # browser decoder matches the actual stream (fixes Safari).
            actual_plid = _camera_profile_level_id.get(self.camera_id, "640028")
            patched_sdp = pc.localDescription.sdp.replace(
                "profile-level-id=42001f", f"profile-level-id={actual_plid}"
            ).replace(
                "profile-level-id=42e01f", f"profile-level-id={actual_plid}"
            )

            # Wait for ICE gathering
            if pc.iceGatheringState != "complete":
                ice_complete = asyncio.Event()

                @pc.on("icegatheringstatechange")
                def _on_ice():
                    if pc.iceGatheringState == "complete":
                        ice_complete.set()

                try:
                    await asyncio.wait_for(ice_complete.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("ICE gathering timeout for viewer [%s]", self.camera_id)

            await self.send(text_data=json.dumps({
                "type": "webrtc_answer",
                "sdp": patched_sdp,
            }))
            logger.info("WebRTC answer sent to viewer for [%s]", self.camera_id)

        except Exception as e:
            logger.error("WebRTC offer handling failed: %s", e, exc_info=True)
            await self.send(text_data=json.dumps({
                "type": "webrtc_error",
                "error": str(e),
            }))

    async def camera_frame(self, event):
        """Video delivered via WebRTC — only forward JPEG fallback over WS."""
        try:
            encoding = event.get("encoding", "jpeg")
            if encoding != "h264":
                b64 = base64.b64encode(event["data"]).decode("ascii")
                await self.send(text_data=json.dumps({"type": "frame", "data": b64}))
        except Exception as e:
            logger.warning(f"Viewer [{getattr(self, 'camera_id', '?')}] frame send error: {e}")

    async def camera_metadata(self, event):
        try:
            await self.send(text_data=event["data"])
            self._meta_count = getattr(self, '_meta_count', 0) + 1
        except Exception as e:
            logger.warning(f"Viewer [{getattr(self, 'camera_id', '?')}] metadata send error: {e}")

    @database_sync_to_async
    def _user_can_access_camera(self, user, camera_id):
        """Check if user is explicitly in the camera's allowed_users."""
        from .models import Camera
        return Camera.objects.filter(
            camera_id=camera_id, allowed_users=user,
        ).exists()
