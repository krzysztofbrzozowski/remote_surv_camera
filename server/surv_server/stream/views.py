import json
import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

import psutil
from django.conf import settings


def _get_net_io():
    """Return network I/O counters for the configured physical interface only.

    Uses settings.NETWORK_INTERFACE if set, otherwise auto-detects the primary
    physical interface (excludes lo, docker*, br-*, veth* which inflate the
    aggregate counters with double/triple-counted Docker bridge traffic).
    """
    iface = getattr(settings, "NETWORK_INTERFACE", "")
    per_nic = psutil.net_io_counters(pernic=True)

    if iface and iface in per_nic:
        return per_nic[iface]

    # Auto-detect: pick the non-virtual interface with most bytes received
    skip_prefixes = ("lo", "docker", "br-", "veth", "virbr", "flannel", "cni", "cali")
    best = None
    best_rx = -1
    for name, counters in per_nic.items():
        if any(name.startswith(p) for p in skip_prefixes):
            continue
        if counters.bytes_recv > best_rx:
            best = counters
            best_rx = counters.bytes_recv

    return best if best is not None else psutil.net_io_counters()


# ---------------------------------------------------------------------------
# SSH auth log parsing (Accepted / Failed attempts from ssh_auth.log)
# ---------------------------------------------------------------------------
# The extract_ssh_auth.sh cron script copies relevant sshd lines from
# /var/log/auth.log to data/ssh_auth.log (/app/data/ssh_auth.log in Docker).
# This avoids mounting the host auth.log into the container as root.
# ---------------------------------------------------------------------------
_ssh_auth_cache = {"entries": [], "ts": 0, "mtime": 0}
_ssh_auth_lock = Lock()
_SSH_CACHE_TTL = 30  # seconds

# Traditional syslog:  "Mar  8 14:30:22 host sshd[123]: Accepted ..."
# ISO 8601 (newer):    "2026-03-08T00:00:34.776338+00:00 host sshd[123]: Accepted ..."
_RE_TIMESTAMP = r"(\S+(?:\s+\d+\s+[\d:]+)?)"  # captures either format

_RE_SSH_ACCEPTED = re.compile(
    _RE_TIMESTAMP + r"\s+\S+\s+sshd\[\d+\]:\s+"
    r"Accepted\s+(\S+)\s+for\s+(\S+)\s+from\s+([\d.]+)\s+port\s+(\d+)"
)
_RE_SSH_FAILED = re.compile(
    _RE_TIMESTAMP + r"\s+\S+\s+sshd\[\d+\]:\s+"
    r"Failed\s+(\S+)\s+for\s+(?:invalid user\s+)?(\S+)\s+from\s+([\d.]+)\s+port\s+(\d+)"
)
# "Invalid user admin from 203.0.113.42 port 44350" — pre-auth rejection
_RE_SSH_INVALID = re.compile(
    _RE_TIMESTAMP + r"\s+\S+\s+sshd\[\d+\]:\s+"
    r"Invalid user\s+(\S+)\s+from\s+([\d.]+)\s+port\s+(\d+)"
)


def _fmt_ssh_time(raw: str) -> str:
    """Normalize an SSH log timestamp to a short readable form."""
    # ISO 8601: "2026-03-08T14:30:22.123456+00:00" → "Mar 08 14:30"
    if "T" in raw:
        try:
            dt = datetime.fromisoformat(raw)
            return dt.strftime("%b %d %H:%M")
        except (ValueError, TypeError):
            return raw[:16]
    # Traditional syslog: "Mar  8 14:30:22" → "Mar 08 14:30"
    parts = raw.split()
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1].zfill(2)} {parts[2][:5]}"
    return raw


def _parse_ssh_auth_log():
    """Return recent SSH auth attempts from the extracted ssh_auth.log.

    On production the file is populated by scripts/extract_ssh_auth.sh
    running via cron with sudo.  Locally it falls back to /var/log/auth.log
    or journalctl.  Results are cached for _SSH_CACHE_TTL seconds.

    Returns list of dicts:
        {"time": str, "status": "accepted"|"failed", "method": str,
         "user": str, "ip": str, "port": str}
    """
    now = time.time()
    with _ssh_auth_lock:
        if now - _ssh_auth_cache["ts"] < _SSH_CACHE_TTL:
            return _ssh_auth_cache["entries"]

    custom = getattr(settings, "SSH_AUTH_LOG_PATH", "")
    candidates = [custom] if custom else []
    # Docker: ./data is mounted as /app/data
    candidates += ["/app/data/ssh_auth.log", "data/ssh_auth.log",
                   "/var/log/auth.log"]

    entries = []
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            st = os.stat(path)
            with _ssh_auth_lock:
                if st.st_mtime == _ssh_auth_cache["mtime"] and _ssh_auth_cache["entries"]:
                    _ssh_auth_cache["ts"] = now
                    return _ssh_auth_cache["entries"]

            with open(path, "r", errors="replace") as fh:
                # Read last ~500 KB to stay fast on large log files
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 512_000))
                if fh.tell() > 0:
                    fh.readline()  # skip partial line
                lines = fh.readlines()

            for line in lines:
                m = _RE_SSH_ACCEPTED.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "accepted",
                        "method": m.group(2), "user": m.group(3),
                        "ip": m.group(4), "port": m.group(5),
                    })
                    continue
                m = _RE_SSH_FAILED.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "failed",
                        "method": m.group(2), "user": m.group(3),
                        "ip": m.group(4), "port": m.group(5),
                    })
                    continue
                m = _RE_SSH_INVALID.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "failed",
                        "method": "invalid_user", "user": m.group(2),
                        "ip": m.group(3), "port": m.group(4),
                    })

            with _ssh_auth_lock:
                _ssh_auth_cache["entries"] = entries
                _ssh_auth_cache["ts"] = now
                _ssh_auth_cache["mtime"] = st.st_mtime
            return entries
        except OSError:
            continue

    # Fallback: journalctl (works on non-Docker hosts with systemd)
    try:
        result = subprocess.run(
            ["journalctl", "-u", "ssh", "-u", "sshd",
             "--since", "24 hours ago", "--no-pager", "-q"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                m = _RE_SSH_ACCEPTED.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "accepted",
                        "method": m.group(2), "user": m.group(3),
                        "ip": m.group(4), "port": m.group(5),
                    })
                    continue
                m = _RE_SSH_FAILED.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "failed",
                        "method": m.group(2), "user": m.group(3),
                        "ip": m.group(4), "port": m.group(5),
                    })
                    continue
                m = _RE_SSH_INVALID.search(line)
                if m:
                    entries.append({
                        "time": _fmt_ssh_time(m.group(1)),
                        "status": "failed",
                        "method": "invalid_user", "user": m.group(2),
                        "ip": m.group(3), "port": m.group(4),
                    })
    except (OSError, subprocess.TimeoutExpired):
        pass

    with _ssh_auth_lock:
        _ssh_auth_cache["entries"] = entries
        _ssh_auth_cache["ts"] = now
        _ssh_auth_cache["mtime"] = 0
    return entries


from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models as db_models
from django.db.models import F as models_F
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django_otp import devices_for_user, user_has_device
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.utils.http import url_has_allowed_host_and_scheme

from surv_server.stream.models import Camera, NetworkHourlyStat, Recording


def _cameras_for_user(user):
    """Return cameras the user is explicitly allowed to access."""
    return Camera.objects.filter(allowed_users=user).distinct()


def _validate_next_url(url: str, request) -> str:
    """Validate redirect URL to prevent open redirect / XSS."""
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return "/"


# Login rate limiting (in-memory, per-process)
_login_attempts = defaultdict(list)
_login_lock = Lock()


def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is rate-limited."""
    now = time.time()
    window = settings.LOGIN_RATELIMIT_WINDOW_SECONDS
    max_attempts = settings.LOGIN_RATELIMIT_MAX_ATTEMPTS

    with _login_lock:
        attempts = _login_attempts[ip]
        # Prune old attempts
        _login_attempts[ip] = [t for t in attempts if now - t < window]
        return len(_login_attempts[ip]) >= max_attempts


def _record_attempt(ip: str):
    with _login_lock:
        _login_attempts[ip].append(time.time())


def _get_client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _validate_recording_filename(filename: str) -> bool:
    """Validate recording filename to prevent path traversal."""
    return bool(settings.RECORDING_FILENAME_PATTERN.match(filename))


def login_view(request):
    error = None
    client_ip = _get_client_ip(request)

    # Already authenticated and OTP-verified — redirect straight to target
    if request.user.is_authenticated and hasattr(request.user, "is_verified") and request.user.is_verified():
        next_url = _validate_next_url(request.GET.get("next", "/"), request)
        return redirect(next_url)

    # OTP verification step — user already passed password auth
    otp_user_id = request.session.get("_2fa_user_id")
    if otp_user_id and request.method == "POST" and "otp_token" in request.POST:
        if _check_rate_limit(client_ip):
            error = "Too many login attempts. Please try again later."
            return render(request, "stream/login_otp.html", {"error": error})

        try:
            user = User.objects.get(pk=otp_user_id)
        except User.DoesNotExist:
            del request.session["_2fa_user_id"]
            return redirect("login")

        otp_token = request.POST.get("otp_token", "").strip()
        for device in devices_for_user(user, confirmed=True):
            if device.verify_token(otp_token):
                next_url = _validate_next_url(request.session.pop("_2fa_next", "/"), request)
                del request.session["_2fa_user_id"]
                login(request, user)
                # Mark OTP-verified so django-otp is_verified() returns True
                request.session["otp_device_id"] = device.persistent_id
                return HttpResponse(
                    f'<html><body><script>window.location.replace("{next_url}")</script></body></html>',
                    content_type="text/html",
                )
        _record_attempt(client_ip)
        error = "Invalid verification code"
        return render(request, "stream/login_otp.html", {"error": error})

    # Show OTP form if pending
    if otp_user_id and request.method == "GET":
        if request.GET.get("reset"):
            del request.session["_2fa_user_id"]
            return redirect("login")
        return render(request, "stream/login_otp.html", {"error": None})

    # Password authentication step
    if request.method == "POST":
        if _check_rate_limit(client_ip):
            error = "Too many login attempts. Please try again later."
            return render(request, "stream/login.html", {"error": error})

        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            next_url = _validate_next_url(request.GET.get("next", "/"), request)
            if user_has_device(user, confirmed=True):
                request.session["_2fa_user_id"] = user.pk
                request.session["_2fa_next"] = next_url
                return render(request, "stream/login_otp.html", {"error": None})
            login(request, user)
            # Use JS redirect instead of 302 to ensure session cookie is set
            # before WebSocket connections on the target page (Safari fix)
            return HttpResponse(
                f'<html><body><script>window.location.replace("{next_url}")</script></body></html>',
                content_type="text/html",
            )
        _record_attempt(client_ip)
        error = "Invalid credentials"
    return render(request, "stream/login.html", {"error": error})


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def live_view(request):
    cameras = _cameras_for_user(request.user)
    selected = request.GET.get("camera")
    if not selected and cameras.exists():
        selected = cameras.first().camera_id
    from django.conf import settings as django_settings
    stun = django_settings.STUN_SERVER
    turn = django_settings.TURN_SERVER
    turn_user = django_settings.TURN_USERNAME
    turn_pass = django_settings.TURN_PASSWORD
    ice_servers = []
    if stun:
        ice_servers.append({"urls": stun})
    if turn:
        # Provide both UDP and TCP TURN — TCP works through stricter firewalls
        ice_servers.append({
            "urls": [turn, turn.replace("?transport=udp", "?transport=tcp")
                     if "?transport=" in turn else turn + "?transport=tcp"],
            "username": turn_user,
            "credential": turn_pass,
        })
    ice_servers_json = json.dumps(ice_servers)
    response = render(request, "stream/live.html", {
        "cameras": cameras,
        "selected_camera": selected,
        "session_key": request.session.session_key or "",
        "ice_servers_json": ice_servers_json,
    })
    # Prevent caching so session_key is always fresh
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    return response


@login_required
def recordings_list(request):
    camera_id = request.GET.get("camera")
    cameras = _cameras_for_user(request.user)
    camera_ids = cameras.values_list("camera_id", flat=True)
    recordings = Recording.objects.select_related("camera").filter(camera__camera_id__in=camera_ids)
    if camera_id:
        recordings = recordings.filter(camera__camera_id=camera_id)
    return render(request, "stream/recordings.html", {
        "recordings": recordings,
        "cameras": cameras,
        "selected_camera": camera_id,
    })


@login_required
def recording_playback(request, filename):
    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    # Ensure resolved path stays within RECORDINGS_DIR
    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")
    if not rec_path.exists():
        raise Http404("Recording file not found")

    return render(request, "stream/playback.html", {
        "recording": recording,
        "filename": filename,
        "is_h264": filename.endswith(".h264"),
        "display_rotation": recording.camera.display_rotation or 0,
    })


@login_required
def recording_download(request, filename):
    """Stream MJPEG frames with adjustable speed via ?speed= query param."""
    import time

    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    # Ensure resolved path stays within RECORDINGS_DIR
    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")
    if not rec_path.exists():
        raise Http404("Recording file not found")

    speed = float(request.GET.get("speed", "1.0"))
    speed = max(0.25, min(speed, 8.0))
    # Base delay per frame at 15 fps
    base_delay = 1.0 / 15.0

    def frame_generator():
        with open(rec_path, "rb") as f:
            while True:
                length_bytes = f.read(4)
                if len(length_bytes) < 4:
                    break
                length = int.from_bytes(length_bytes, "big")
                frame = f.read(length)
                if len(frame) < length:
                    break
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" +
                    frame +
                    b"\r\n"
                )
                time.sleep(base_delay / speed)

    response = StreamingHttpResponse(
        frame_generator(),
        content_type="multipart/x-mixed-replace; boundary=frame"
    )
    return response


@login_required
def recording_frame(request, filename, frame_number):
    """Serve a single JPEG frame from a recording by frame index."""
    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")
    if not rec_path.exists():
        raise Http404("Recording file not found")

    frame_number = max(0, int(frame_number))

    with open(rec_path, "rb") as f:
        current = 0
        while True:
            length_bytes = f.read(4)
            if len(length_bytes) < 4:
                raise Http404("Frame not found")
            length = int.from_bytes(length_bytes, "big")
            if current == frame_number:
                frame = f.read(length)
                if len(frame) < length:
                    raise Http404("Frame corrupted")
                from django.http import HttpResponse
                return HttpResponse(frame, content_type="image/jpeg")
            f.seek(length, 1)
            current += 1


@login_required
def api_cameras(request):
    """JSON API: list cameras with online status."""
    cameras = _cameras_for_user(request.user)
    data = [{
        "camera_id": c.camera_id,
        "name": c.name or c.camera_id,
        "is_online": c.is_online,
        "last_seen": c.last_seen.isoformat() if c.last_seen else None,
    } for c in cameras]
    return JsonResponse(data, safe=False)


@login_required
def recording_download_file(request, filename):
    """Download the raw recording file."""
    from django.http import FileResponse

    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")
    if not rec_path.exists():
        raise Http404("Recording file not found")

    return FileResponse(
        open(rec_path, "rb"),
        as_attachment=True,
        filename=filename,
        content_type="application/octet-stream",
    )


@login_required
def recording_h264_mp4(request, filename):
    """Remux raw H.264 recording to MP4 for browser playback (no re-encode)."""
    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")
    if not filename.endswith(".h264"):
        raise Http404("Not an H.264 recording")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")
    if not rec_path.exists():
        raise Http404("Recording file not found")

    # Check for cached MP4
    mp4_path = rec_path.with_suffix(".mp4")
    if not mp4_path.exists():
        # Compute average FPS from recording metadata to fix VideoToolbox timestamp bug
        avg_fps = 25.0
        if recording.ended_at and recording.started_at and recording.frame_count:
            elapsed = (recording.ended_at - recording.started_at).total_seconds()
            if elapsed > 0:
                avg_fps = max(1.0, min(60.0, recording.frame_count / elapsed))

        # Get display_rotation from camera model for MP4 rotation metadata
        rotation = recording.camera.display_rotation or 0

        ffmpeg_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-r", f"{avg_fps:.3f}",
            "-i", str(rec_path), "-c", "copy", "-movflags", "+faststart",
        ]
        if rotation:
            ffmpeg_cmd += ["-metadata:s:v", f"rotate={rotation}"]
        ffmpeg_cmd.append(str(mp4_path))

        result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not mp4_path.exists():
            raise Http404("MP4 conversion failed")

    # Serve MP4 — download if ?download=1, otherwise inline playback
    is_download = request.GET.get("download") == "1"
    file_size = mp4_path.stat().st_size
    range_header = request.META.get("HTTP_RANGE")

    if range_header:
        import re as _re
        m = _re.match(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            length = end - start + 1

            f = open(mp4_path, "rb")
            f.seek(start)
            resp = HttpResponse(f.read(length), content_type="video/mp4", status=206)
            resp["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            resp["Content-Length"] = str(length)
            resp["Accept-Ranges"] = "bytes"
            f.close()
            return resp

    from django.http import FileResponse
    mp4_name = filename.replace(".h264", ".mp4")
    resp = FileResponse(
        open(mp4_path, "rb"),
        content_type="video/mp4",
        filename=mp4_name,
    )
    resp["Accept-Ranges"] = "bytes"
    if is_download:
        resp["Content-Disposition"] = f'attachment; filename="{mp4_name}"'
    return resp


@login_required
@login_required
def recording_delete(request, filename):
    """Delete a recording file and its database entry. Superuser only."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required")
    if request.method != "POST":
        return HttpResponseForbidden("POST required")

    if not _validate_recording_filename(filename):
        raise Http404("Invalid filename")

    recording = Recording.objects.select_related("camera").filter(filename=filename).first()
    if not recording:
        raise Http404("Recording not found")

    camera_id = recording.camera.camera_id
    rec_path = (Path(settings.RECORDINGS_DIR) / camera_id / filename).resolve()

    if not str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
        raise Http404("Recording not found")

    # Delete file from disk
    if rec_path.exists():
        rec_path.unlink()

    # Delete database record
    recording.delete()

    return redirect("recordings")


@login_required
def recording_delete_all(request):
    """Delete all recordings, optionally filtered by camera. Superuser only."""
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required")
    if request.method != "POST":
        return HttpResponseForbidden("POST required")

    camera_id = request.POST.get("camera", "")
    cameras = _cameras_for_user(request.user)
    camera_ids = cameras.values_list("camera_id", flat=True)
    recordings = Recording.objects.select_related("camera").filter(camera__camera_id__in=camera_ids)
    if camera_id:
        recordings = recordings.filter(camera__camera_id=camera_id)

    for recording in recordings:
        rec_path = (Path(settings.RECORDINGS_DIR) / recording.camera.camera_id / recording.filename).resolve()
        if str(rec_path).startswith(str(Path(settings.RECORDINGS_DIR).resolve())):
            if rec_path.exists():
                rec_path.unlink()
            # Also remove cached MP4
            mp4_path = rec_path.with_suffix(".mp4")
            if mp4_path.exists():
                mp4_path.unlink()
        recording.delete()

    redirect_url = "recordings"
    return redirect(redirect_url)


@login_required
def admin_stats_view(request):
    """Admin statistics page with server health graphs."""
    return render(request, "stream/admin_stats.html")


@login_required
def api_server_stats(request):
    """JSON API: server health stats for the admin dashboard."""
    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=0)
    cpu_per_core = psutil.cpu_percent(interval=0, percpu=True)

    # Memory
    mem = psutil.virtual_memory()

    # Disk usage for recordings dir
    disk = psutil.disk_usage(str(settings.RECORDINGS_DIR))

    # Active connections via psutil
    active_connections = []
    try:
        for conn in psutil.net_connections(kind='tcp'):
            if conn.status == 'ESTABLISHED' and conn.laddr and conn.laddr.port == 8000 and conn.raddr:
                active_connections.append({
                    "remote_ip": conn.raddr.ip,
                    "remote_port": conn.raddr.port,
                    "pid": conn.pid or "?",
                })
    except (psutil.AccessDenied, OSError):
        pass

    # SSH connections via ss -tnp (real SSH sessions)
    ssh_sessions = []
    try:
        result = subprocess.run(
            ["ss", "-tnp"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:  # skip header
                parts = line.split()
                if len(parts) >= 5 and ":22" in parts[3]:
                    local_addr = parts[3]
                    peer_addr = parts[4]
                    process_info = parts[5] if len(parts) > 5 else ""
                    ssh_sessions.append({
                        "local": local_addr,
                        "peer": peer_addr,
                        "process": process_info,
                    })
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Network I/O
    net_io = _get_net_io()
    _accumulate_hourly_stats(net_io, cpu_percent, mem.percent)
    net_recv_mb_total = round(net_io.bytes_recv / (1024 * 1024), 1)
    net_sent_mb_total = round(net_io.bytes_sent / (1024 * 1024), 1)

    # Compute 24H network delta from history ring buffer
    net_recv_mb_24h = net_recv_mb_total
    net_sent_mb_24h = net_sent_mb_total
    cutoff_24h = time.time() - 86400
    _stats_history_lock.acquire()
    try:
        for s in _stats_history:
            if s["t"] >= cutoff_24h:
                net_recv_mb_24h = round(net_recv_mb_total - s["net_recv"], 1)
                net_sent_mb_24h = round(net_sent_mb_total - s["net_sent"], 1)
                break
    finally:
        _stats_history_lock.release()

    # Recording data stats
    total_recordings = Recording.objects.count()
    total_recording_bytes = Recording.objects.aggregate(
        total=db_models.Sum("size_bytes")
    )["total"] or 0

    # Server uptime
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time

    now = time.time()
    data = {
        "cpu": {
            "percent": cpu_percent,
            "per_core": cpu_per_core,
            "count": psutil.cpu_count(),
        },
        "memory": {
            "total_mb": round(mem.total / (1024 * 1024), 1),
            "used_mb": round(mem.used / (1024 * 1024), 1),
            "available_mb": round(mem.available / (1024 * 1024), 1),
            "percent": mem.percent,
        },
        "disk": {
            "total_gb": round(disk.total / (1024 ** 3), 1),
            "used_gb": round(disk.used / (1024 ** 3), 1),
            "free_gb": round(disk.free / (1024 ** 3), 1),
            "percent": disk.percent,
        },
        "network": {
            "bytes_sent": net_io.bytes_sent,
            "bytes_recv": net_io.bytes_recv,
            "bytes_sent_mb": net_sent_mb_24h,
            "bytes_recv_mb": net_recv_mb_24h,
            "bytes_sent_mb_total": net_sent_mb_total,
            "bytes_recv_mb_total": net_recv_mb_total,
        },
        "ssh_sessions": ssh_sessions,
        "ssh_count": len(ssh_sessions),
        "ssh_auth_attempts": _parse_ssh_auth_log(),
        "active_connections": active_connections,
        "active_connection_count": len(active_connections),
        "recordings": {
            "count": total_recordings,
            "total_size_mb": round(total_recording_bytes / (1024 * 1024), 1),
        },
        "uptime_seconds": int(uptime_seconds),
        "timestamp": now,
    }

    # Store sample in server-side history ring buffer
    _stats_history_lock.acquire()
    try:
        _stats_history.append({
            "t": now,
            "cpu": cpu_percent,
            "mem": mem.percent,
            "net_recv": net_recv_mb_total,
            "net_sent": net_sent_mb_total,
        })
    finally:
        _stats_history_lock.release()

    return JsonResponse(data)


# Server-side stats history (ring buffer, keeps 24h of 5s samples = ~17280 entries)
from collections import deque
from threading import Lock

_stats_history = deque(maxlen=17280)
_stats_history_lock = Lock()


@login_required
def api_stats_history(request):
    """JSON API: return stats history for a given time window."""
    window = request.GET.get("window", "1m")
    windows = {"1m": 60, "10m": 600, "1h": 3600, "24h": 86400}
    seconds = windows.get(window, 60)
    cutoff = time.time() - seconds

    _stats_history_lock.acquire()
    try:
        samples = [s for s in _stats_history if s["t"] >= cutoff]
    finally:
        _stats_history_lock.release()

    return JsonResponse({"window": window, "samples": samples})


# ---------------------------------------------------------------------------
# Hourly stats persistence (network, CPU, memory from psutil)
# ---------------------------------------------------------------------------
_last_net_snapshot = {"rx": 0, "tx": 0, "t": 0}


def _accumulate_hourly_stats(net_io, cpu_percent, mem_percent):
    """Called on each 5s poll: persist network deltas + CPU/memory snapshots to current hour."""
    global _last_net_snapshot
    now_rx = net_io.bytes_recv
    now_tx = net_io.bytes_sent

    prev_rx = _last_net_snapshot["rx"]
    prev_tx = _last_net_snapshot["tx"]
    prev_t = _last_net_snapshot["t"]

    _last_net_snapshot = {"rx": now_rx, "tx": now_tx, "t": time.time()}

    today = date.today()
    hour = datetime.now().hour

    # Compute network deltas (skip first call or counter resets)
    delta_rx = 0
    delta_tx = 0
    has_net_delta = False
    if prev_t != 0 and now_rx >= prev_rx and now_tx >= prev_tx:
        delta_rx = now_rx - prev_rx
        delta_tx = now_tx - prev_tx
        # Skip unreasonably large deltas (> 1 GB in 5s)
        if delta_rx <= 1_073_741_824 and delta_tx <= 1_073_741_824:
            has_net_delta = True

    try:
        obj, created = NetworkHourlyStat.objects.get_or_create(
            date=today, hour=hour,
            defaults={
                "rx_bytes": delta_rx if has_net_delta else 0,
                "tx_bytes": delta_tx if has_net_delta else 0,
                "cpu_sum": cpu_percent,
                "mem_sum": mem_percent,
                "sample_count": 1,
            },
        )
        if not created:
            updates = {
                "cpu_sum": models_F("cpu_sum") + cpu_percent,
                "mem_sum": models_F("mem_sum") + mem_percent,
                "sample_count": models_F("sample_count") + 1,
            }
            if has_net_delta and (delta_rx > 0 or delta_tx > 0):
                updates["rx_bytes"] = models_F("rx_bytes") + delta_rx
                updates["tx_bytes"] = models_F("tx_bytes") + delta_tx
            NetworkHourlyStat.objects.filter(pk=obj.pk).update(**updates)
    except Exception:
        pass  # Don't break stats endpoint on DB errors


# ---------------------------------------------------------------------------
# Background stats collector — runs every 30s regardless of active viewers
# ---------------------------------------------------------------------------
_bg_stats_started = False
_bg_stats_lock = Lock()


def start_background_stats_collector():
    """Start a daemon thread that collects CPU/memory/network stats every 30s.

    Safe to call multiple times — only the first call actually starts the thread.
    """
    global _bg_stats_started
    with _bg_stats_lock:
        if _bg_stats_started:
            return
        _bg_stats_started = True

    import threading
    import logging

    logger = logging.getLogger("server")

    def _collector_loop():
        logger.info("Background stats collector started (30s interval)")
        while True:
            try:
                time.sleep(30)
                net_io = _get_net_io()
                cpu_pct = psutil.cpu_percent(interval=None)
                mem_pct = psutil.virtual_memory().percent
                _accumulate_hourly_stats(net_io, cpu_pct, mem_pct)
            except Exception as exc:
                logger.warning(f"Background stats collector error: {exc}")

    t = threading.Thread(target=_collector_loop, daemon=True, name="bg-stats-collector")
    t.start()


@login_required
def api_network_usage(request):
    """JSON API: server stats for Apple Screen Time-style dashboard (network, CPU, memory)."""
    today = date.today()
    weekday = today.weekday()  # 0=Monday
    week_start = today - timedelta(days=weekday)
    week_end = week_start + timedelta(days=6)
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = week_start - timedelta(days=1)

    fields = ("date", "hour", "rx_bytes", "tx_bytes", "cpu_sum", "mem_sum", "sample_count")

    # Current week hourly data
    current_week_stats = list(
        NetworkHourlyStat.objects.filter(
            date__gte=week_start, date__lte=week_end
        ).values(*fields).order_by("date", "hour")
    )

    # Previous week for % comparison
    prev_week_stats = list(
        NetworkHourlyStat.objects.filter(
            date__gte=prev_week_start, date__lte=prev_week_end
        ).values(*fields)
    )

    def _hourly_for_day(stats, target_date):
        hourly = []
        for h in range(24):
            matches = [s for s in stats if s["date"] == target_date and s["hour"] == h]
            rx = sum(s["rx_bytes"] for s in matches)
            tx = sum(s["tx_bytes"] for s in matches)
            cpu_s = sum(s["cpu_sum"] for s in matches)
            mem_s = sum(s["mem_sum"] for s in matches)
            cnt = sum(s["sample_count"] for s in matches)
            hourly.append({
                "hour": h,
                "rx_bytes": rx, "tx_bytes": tx,
                "cpu_avg": round(cpu_s / cnt, 1) if cnt else 0,
                "mem_avg": round(mem_s / cnt, 1) if cnt else 0,
            })
        return hourly

    # Build daily totals + per-day hourly for current week (M-S)
    day_labels = ["M", "T", "W", "T", "F", "S", "S"]
    daily_totals = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        day_stats = [s for s in current_week_stats if s["date"] == d]
        rx = sum(s["rx_bytes"] for s in day_stats)
        tx = sum(s["tx_bytes"] for s in day_stats)
        cpu_s = sum(s["cpu_sum"] for s in day_stats)
        mem_s = sum(s["mem_sum"] for s in day_stats)
        cnt = sum(s["sample_count"] for s in day_stats)
        daily_totals.append({
            "date": d.isoformat(),
            "day_label": day_labels[i],
            "rx_bytes": rx, "tx_bytes": tx,
            "cpu_avg": round(cpu_s / cnt, 1) if cnt else 0,
            "mem_avg": round(mem_s / cnt, 1) if cnt else 0,
            "hourly": _hourly_for_day(current_week_stats, d),
        })

    # Week totals
    week_rx = sum(d["rx_bytes"] for d in daily_totals)
    week_tx = sum(d["tx_bytes"] for d in daily_totals)
    days_with_data = sum(1 for d in daily_totals if d["rx_bytes"] > 0 or d["tx_bytes"] > 0 or d["cpu_avg"] > 0)
    daily_avg_rx = week_rx // max(days_with_data, 1)
    daily_avg_tx = week_tx // max(days_with_data, 1)

    # CPU/mem weekly averages
    all_cpu = [d["cpu_avg"] for d in daily_totals if d["cpu_avg"] > 0]
    all_mem = [d["mem_avg"] for d in daily_totals if d["mem_avg"] > 0]
    week_cpu_avg = round(sum(all_cpu) / len(all_cpu), 1) if all_cpu else 0
    week_mem_avg = round(sum(all_mem) / len(all_mem), 1) if all_mem else 0

    # Previous week for % change
    prev_rx = sum(s["rx_bytes"] for s in prev_week_stats)
    prev_tx = sum(s["tx_bytes"] for s in prev_week_stats)
    prev_days_with_data = len(set(s["date"] for s in prev_week_stats if s["rx_bytes"] > 0 or s["tx_bytes"] > 0 or s["sample_count"] > 0))
    prev_daily_avg_rx = prev_rx // max(prev_days_with_data, 1)
    prev_daily_avg_tx = prev_tx // max(prev_days_with_data, 1)

    prev_cpu = [s for s in prev_week_stats if s["sample_count"] > 0]
    prev_cpu_avg = round(sum(s["cpu_sum"] / s["sample_count"] for s in prev_cpu) / len(prev_cpu), 1) if prev_cpu else 0
    prev_mem_avg = round(sum(s["mem_sum"] / s["sample_count"] for s in prev_cpu) / len(prev_cpu), 1) if prev_cpu else 0

    def pct_change(current, previous):
        if previous == 0:
            return None
        return round((current - previous) / previous * 100)

    # Today's data
    today_hourly = _hourly_for_day(current_week_stats, today)
    today_rx = sum(h["rx_bytes"] for h in today_hourly)
    today_tx = sum(h["tx_bytes"] for h in today_hourly)
    today_cpu_vals = [h["cpu_avg"] for h in today_hourly if h["cpu_avg"] > 0]
    today_mem_vals = [h["mem_avg"] for h in today_hourly if h["mem_avg"] > 0]

    return JsonResponse({
        "today": {
            "date": today.isoformat(),
            "date_label": today.strftime("%d %B"),
            "rx_bytes": today_rx,
            "tx_bytes": today_tx,
            "cpu_avg": round(sum(today_cpu_vals) / len(today_cpu_vals), 1) if today_cpu_vals else 0,
            "mem_avg": round(sum(today_mem_vals) / len(today_mem_vals), 1) if today_mem_vals else 0,
            "hourly": today_hourly,
        },
        "week": {
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
            "daily": daily_totals,
            "total_rx_bytes": week_rx,
            "total_tx_bytes": week_tx,
            "daily_avg_rx_bytes": daily_avg_rx,
            "daily_avg_tx_bytes": daily_avg_tx,
            "pct_change_rx": pct_change(daily_avg_rx, prev_daily_avg_rx),
            "pct_change_tx": pct_change(daily_avg_tx, prev_daily_avg_tx),
            "cpu_avg": week_cpu_avg,
            "mem_avg": week_mem_avg,
            "pct_change_cpu": pct_change(week_cpu_avg, prev_cpu_avg),
            "pct_change_mem": pct_change(week_mem_avg, prev_mem_avg),
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_camera_token(request):
    """DRF API: return the current camera token for authenticated users."""
    return Response({"camera_token": settings.CAMERA_TOKEN})


# ---------------------------------------------------------------------------
# Admin Logs
# ---------------------------------------------------------------------------

import logging as _logging

# In-memory ring buffer for server logs
_log_buffer = deque(maxlen=2000)
_log_buffer_lock = Lock()


class _BufferHandler(_logging.Handler):
    """Captures log records into the in-memory ring buffer."""

    def emit(self, record):
        try:
            entry = {
                "t": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": self.format(record),
            }
            with _log_buffer_lock:
                _log_buffer.append(entry)
        except Exception:
            pass


# Install the buffer handler on the root logger so all server logs are captured
_buffer_handler = _BufferHandler()
_buffer_handler.setLevel(_logging.INFO)
_buffer_handler.setFormatter(
    _logging.Formatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                       datefmt="%H:%M:%S")
)
_logging.getLogger().addHandler(_buffer_handler)


@login_required
def admin_logs_view(request):
    """Admin logs page with real-time log display."""
    return render(request, "stream/admin_logs.html")


@login_required
def api_server_logs(request):
    """JSON API: return recent server log entries."""
    since = request.GET.get("since")
    level = request.GET.get("level", "").upper()

    with _log_buffer_lock:
        entries = list(_log_buffer)

    if since:
        try:
            since_ts = float(since)
            entries = [e for e in entries if e["t"] > since_ts]
        except (ValueError, TypeError):
            pass

    if level and level != "ALL":
        entries = [e for e in entries if e["level"] == level]

    # Return last 500 max
    entries = entries[-500:]

    return JsonResponse({"logs": entries})


# ---------------------------------------------------------------------------
# Daily Log File Browser
# ---------------------------------------------------------------------------

_LOGS_DIR = Path(settings.BASE_DIR) / "logs"


@login_required
def admin_daily_logs_view(request):
    """Browse daily log directories and files."""
    days = []
    if _LOGS_DIR.is_dir():
        for d in sorted(_LOGS_DIR.iterdir(), reverse=True):
            if d.is_dir() and len(d.name) == 10:  # YYYY-MM-DD
                files = sorted(f.name for f in d.iterdir() if f.is_file())
                days.append({"date": d.name, "files": files})
    return render(request, "stream/admin_daily_logs.html", {"days": days})


@login_required
def admin_daily_log_file(request, date, filename):
    """Serve a specific daily log file content (sanitized)."""
    import re as _re

    # Validate date format and filename to prevent path traversal
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise Http404("Invalid date")
    if not _re.match(r"^[\w.-]+$", filename):
        raise Http404("Invalid filename")

    log_path = (_LOGS_DIR / date / filename).resolve()
    if not str(log_path).startswith(str(_LOGS_DIR.resolve())):
        raise Http404("Not found")
    if not log_path.is_file():
        raise Http404("File not found")

    from daily_logger import sanitize
    content = log_path.read_text(errors="replace")
    content = sanitize(content)

    # Raw mode for AJAX live-tail
    if request.GET.get("raw") == "1":
        # Return last portion for live tail (last ~100KB)
        tail = content[-102400:] if len(content) > 102400 else content
        return HttpResponse(tail, content_type="text/plain; charset=utf-8")

    return render(request, "stream/admin_daily_log_file.html", {
        "date": date,
        "filename": filename,
        "content": content,
    })
