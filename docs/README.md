# Remote Surveillance Camera

Environmental monitoring is available in the **Climate** tab, with ESP8266 / BME280
ingestion, private sensor history and CSV export. See [sensor setup and API](climate.md).

Multi-camera surveillance system. Raspberry Pi (or macOS) streamers capture and H.264-encode video, send it over WebSocket to a Django server, which delivers live video to browsers via **WebRTC** (aiortc SFU with H.264 passthrough — zero decode/re-encode on the server). Metadata flows over WebSocket. Everything is recorded in chunked H.264 files.

---

## Architecture

Reference -> [architecure](./architecture.md)

### Network Data Flow (Production)

Reference -> [network_data_flow](./network_data_flow.md)

### Data Flow (Internal)

| Step | What happens |
|---|---|
| **Capture** | Camera sensor runs at full FPS (25 fps on RPi); output is software-gated (no sensor restart on FPS change) |
| **Detect** | Lores stream (640×480) frame differencing; changed pixel count > threshold triggers motion state |
| **FPS switch** | Configurable `idle_fps` (default 1) when idle → `fps` (default 25) on motion; reverts after cooldown (default 3 s) |
| **Annotate** | Yellow timestamp (`YYYY-MM-DD HH:MM:SS`) burned into frame before encoding via OpenCV |
| **Encode** | H.264 hardware encoder (V4L2 M2M on RPi / VideoToolbox on macOS / libx264 fallback); `iperiod=15` |
| **Keyframe** | At idle FPS, only keyframes (IDR) are sent — P-frames are dropped to prevent browser blackouts. `force_key_frame()` called after each idle send to guarantee the next frame is an IDR |
| **Frame** | Reader thread splits raw Annex B output into complete **access units**: `SPS+PPS+SEI+IDR` for keyframes, individual slice NALs for P-frames |
| **Send** | Streamer sends two WebSocket messages per frame: JSON metadata first, then binary NAL bytes |
| **Auth** | Server validates `Authorization: Token <camera_token>` on the camera WS connection; per-device token checked first, global `CAMERA_TOKEN` env var as fallback |
| **Ingest** | `CameraConsumer` receives H.264 bytes and pushes to the WebRTC `CameraVideoTrack` queue |
| **Passthrough** | `CameraVideoTrack.recv()` wraps raw H.264 as `av.Packet` — no decoding. aiortc's `pack()` path splits Annex B into NALUs and creates RTP payloads directly |
| **Relay** | `MediaRelay` fans out the `av.Packet` to per-viewer `RTCPeerConnection` subscriptions |
| **Deliver** | aiortc packetizes H.264 NALUs into RTP (FU-A fragmentation for large NALUs) and sends via WebRTC to each browser. Zero re-encoding — true SFU passthrough |
| **Metadata** | JSON metadata relayed over WebSocket (Redis channel layer fan-out) — not via WebRTC |
| **Record** | Raw Annex B H.264 appended to disk; new file opened every 100 MB |
| **Display** | Browser `RTCPeerConnection` receives video track → `<video>` element renders natively |
| **Playback** | Recordings remuxed to MP4 on first request via `ffmpeg -c copy` (no re-encode, cached); rotation metadata embedded for correct orientation on all players |

---

## Repository Layout

```
remote_surv_camera/
├── rpi_streamer/               # Streamer (runs on RPi or macOS)
│   ├── main.py                 # Streamer entry point + WebSocket client
│   ├── sources/
│   │   ├── base.py             # CameraSource ABC
│   │   ├── synthetic.py        # Generated test pattern
│   │   ├── macos.py            # macOS webcam, JPEG encoding
│   │   ├── macos_h264.py       # macOS webcam, H.264 via ffmpeg
│   │   ├── rpi_regular.py      # RPi Camera Module 3, H.264 via picamera2
│   │   ├── rpi_ai.py           # RPi AI Camera (IMX500) hardware detection
│   │   ├── h264_output.py      # picamera2 H.264 StreamOutput helper
│   │   └── ina219_power.py     # INA219 power monitor (optional)
│   ├── configs/
│   │   ├── macos-h264.example.yaml # macOS H.264 local dev config
│   │   ├── macos-cam.example.yaml # macOS JPEG config
│   │   ├── rpi-cam.example.yaml # RPi regular camera config
│   │   ├── rpi-ai-cam.example.yaml # RPi AI camera config
│   │   └── synthetic.example.yaml # Synthetic test config
│   └── requirements.txt
│
├── server/                     # Django + Daphne + Redis (Docker)
│   ├── surv_server/
│   │   ├── stream/
│   │   │   ├── consumers.py    # CameraConsumer + ViewerConsumer (WebRTC signaling)
│   │   │   ├── webrtc.py       # WebRTC SFU: CameraVideoTrack, MediaRelay, PC management
│   │   │   ├── views.py        # Login, live, recordings, playback, MP4 remux, admin logs
│   │   │   ├── models.py       # Camera (token, rotation, retention), Recording models
│   │   │   ├── admin.py        # Django admin: token generation, storage quota display
│   │   │   ├── middleware.py   # Camera token auth (per-device + global fallback)
│   │   │   ├── management/
│   │   │   │   └── commands/
│   │   │   │       └── prune_recordings.py  # Delete oldest recordings over quota
│   │   │   └── routing.py      # WebSocket URL routing
│   │   ├── templates/stream/
│   │   │   ├── live.html       # Live viewer (WebRTC + stats)
│   │   │   ├── recordings.html # Recordings list
│   │   │   ├── playback.html   # Recording playback (<video> for H.264)
│   │   │   ├── admin_logs.html # Real-time server log viewer
│   │   │   ├── login.html
│   │   │   └── base.html       # Navbar with Admin dropdown (Stats | Logs)
│   │   └── settings.py
│   ├── setup_venv.sh                    # Create Python venv for host-mode dev
│   ├── docker-compose.yml               # Network mode (WebRTC + Nginx, host networking)
│   ├── docker-compose.prod.yml          # Production (VPS + reverse proxy bridge)
│   ├── nginx-host.conf                  # Nginx for network mode
│   ├── nginx-proxy-bridge.conf          # Nginx for production proxy bridge
│   └── Dockerfile
│
├── manage_server.sh            # Root wrapper → server/manage.sh
├── manage_rpi_streamer.sh      # Streamer launcher + venv setup + auto-start
├── tests/
│   └── stream_diagnostics.py   # Live stream quality analyzer
└── README.md
```

---

## Camera Sources

| `camera_source` | Hardware | Encoding | Motion detection |
|---|---|---|---|
| `synthetic` | None — generated pattern | JPEG | Simulated |
| `macos` | macOS/USB webcam via OpenCV | JPEG | Frame differencing |
| `macos_h264` | macOS/USB webcam via OpenCV | H.264 (VideoToolbox or libx264) | Frame differencing |
| `rpi` | RPi Camera Module 3 via picamera2 | H.264 (V4L2 M2M hardware) | Frame differencing on lores stream |
| `rpi_ai` | RPi AI Camera (IMX500) | JPEG | Hardware on-sensor detection |

---

## Configuration Reference

All streamer options are set in a YAML file (path passed via `STREAMER_CONFIG` env var):

```yaml
# Connection
server_url:    "ws://192.0.2.10/ws/camera/feed/"  # WebSocket URL
camera_token:  "<token>"                              # Generated in Django Admin → Cameras → Generate token
camera_id:     "rpi-cam"                              # Unique camera name

# Source
camera_source: "rpi"          # synthetic | macos | macos_h264 | rpi | rpi_ai
frame_width:   1920
frame_height:  1080

# FPS
fps:           25             # FPS when motion is active
idle_fps:      1              # FPS when idle

# Encoding
h264_bitrate:  2000000        # bits/s (H.264 sources only)
jpeg_quality:  70             # 1–100 (JPEG sources only)

# Rotation (degrees — physical mounting angle)
camera_rotation: 270          # 0 | 90 | 180 | 270

# Motion detection tuning (optional)
motion_threshold:    25       # Pixel diff threshold (default 25)
min_motion_area:     500      # Min changed pixels to trigger (default 500)
detection_cooldown:  3.0      # Seconds at high FPS after motion stops (default 3.0)
```

---

## Running Locally (macOS)

### Prerequisites

- Docker Desktop
- Python 3.10+
- ffmpeg with VideoToolbox: `brew install ffmpeg`
- Built-in or USB webcam

### 1 — Start the server

```bash
cd server
docker compose up --build -d
# Admin user is created automatically (admin / admin)
```

Open **http://localhost:8000** and log in with `admin` / `admin`.

> **WebRTC on macOS:** Docker Desktop cannot forward UDP ports, so WebRTC won't connect through Docker. For local WebRTC development, use the quick-start command:
>
> ```bash
> ./manage_server.sh local-dev
> ```
>
> Or manually:
>
> ```bash
> cd server
> ./setup_venv.sh                          # One-time: create venv + install deps
> source venv/bin/activate
> export DJANGO_SECRET_KEY='dev-key' CAMERA_TOKEN='<token>' DJANGO_ALLOWED_HOSTS='*' DJANGO_DEBUG=1
> python manage.py migrate && python manage.py createsuperuser
> daphne -b 0.0.0.0 -p 8080 surv_server.asgi:application
> ```
>
> Redis must still be running (e.g., `docker compose up -d redis`).

### 2 — Start the streamer

```bash
cd rpi_streamer
pip3 install -r requirements.txt
(umask 077; cp configs/macos-h264.example.yaml configs/macos-h264.yaml)
# Set server_url and your camera token in the local file.
STREAMER_CONFIG=configs/macos-h264.yaml python3 main.py
```

The streamer uses your webcam at **1920×1080**, encodes with VideoToolbox H.264, and streams at **idle_fps idle → fps on motion** (configurable in YAML).

### 3 — Watch

Go to **http://localhost:8000** — the `mac-cam` stream appears live. Wave your hand in front of the camera to trigger motion mode.

---

## Running on Raspberry Pi

### Prerequisites

- Raspberry Pi OS (Bookworm or newer)
- Camera Module 3 connected
- `picamera2` installed (ships with Raspberry Pi OS)

### 1 — Deploy server (on RPi or separate machine)

```bash
cd server
./manage.sh start              # WebRTC + Nginx on :80 (network mode, recommended)
```

### 2 — Configure the streamer

**Generate a per-device token:** Go to **Django Admin** → **Cameras** → select the camera → **Actions: Generate new auth token** → copy the token.

Create a private configuration from the published template:

```bash
(umask 077; cp rpi_streamer/configs/rpi-cam.example.yaml rpi_streamer/configs/rpi-cam.yaml)
```

Edit `rpi_streamer/configs/rpi-cam.yaml`:

```yaml
server_url:    "ws://192.168.1.x/ws/camera/feed/"  # Your server IP
camera_token:  "<token>"                             # From manage_server.sh setup
camera_id:     "rpi-cam"
camera_source: "rpi"
frame_width:   1920
frame_height:  1080
fps:           25
idle_fps:      1
camera_rotation: 270   # Adjust for physical mounting
```

Or use the interactive setup:

```bash
./manage_rpi_streamer.sh setup-token   # Fetches token from server API
```

### 3 — Set up Python environment

```bash
./manage_rpi_streamer.sh setup-venv        # Creates venv + installs requirements
```

### 4 — Start the streamer

```bash
./manage_rpi_streamer.sh rpi               # Run interactively (foreground)
```

### 5 — Auto-start on boot (recommended for production)

```bash
./manage_rpi_streamer.sh enable-startup rpi   # Enable auto-start via systemd
./manage_rpi_streamer.sh startup-status       # Check service status + logs
./manage_rpi_streamer.sh startup-logs 100     # View last 100 lines of logs
./manage_rpi_streamer.sh disable-startup      # Remove auto-start
```

Uses a **systemd user service** (`~/.config/systemd/user/rpi-streamer.service`) with `loginctl enable-linger` so the streamer starts at boot without requiring login. The service auto-restarts on failure (5s delay, max 10 restarts per 5 min). Logs go to journald (viewable via `startup-logs`).

---

## Server Management

```bash
# From repo root (or cd server && ./manage.sh ...)
./manage_server.sh init-prod                 # Interactive production .env setup
./manage_server.sh start                    # Network mode — WebRTC + Nginx :80 (default)
./manage_server.sh start network            # Same as above
./manage_server.sh start prod               # Production — nginx-proxy + SSL + acme-companion
./manage_server.sh stop
./manage_server.sh restart [mode]
./manage_server.sh rebuild [mode]           # Rebuild Docker image and restart
./manage_server.sh status                   # Show running containers + ports
./manage_server.sh logs [server|redis]      # Tail logs
./manage_server.sh shell                    # Django manage.py shell
./manage_server.sh import-recordings        # Scan disk and register recordings in DB
./manage_server.sh nuke                     # ⚠️  Remove ALL Docker data (irreversible)
./manage_server.sh local-dev                # Run server locally (no Docker) on port 8080
```

### Local Development (No Docker)

```bash
./manage_server.sh local-dev
```

This activates the Python venv, sets dev environment variables, runs migrations, auto-creates an `admin`/`admin` superuser, and starts daphne on `0.0.0.0:8080`. Redis must still be running separately (`docker compose up -d redis`).

### Two-Factor Authentication (2FA)

The Django admin site is protected by TOTP-based two-factor authentication. Any TOTP authenticator app works (Google Authenticator, Apple Passwords, 1Password, Authy).

**Setup flow:**

1. Log in at `/account/login/` (or get redirected when accessing the admin panel)
2. Navigate to `/account/two_factor/setup/` and scan the QR code with your authenticator app
3. Enter the 6-digit code to confirm — 2FA is now active
4. Generate backup tokens at `/account/two_factor/` → Manage Tokens

**Session duration:** Once authenticated with 2FA, the session lasts 30 days before re-authentication is required.

**Key URLs:**

| URL | Purpose |
|---|---|
| `/account/login/` | 2FA login page |
| `/account/two_factor/` | Account security overview |
| `/account/two_factor/setup/` | Enable 2FA |
| `/account/two_factor/backup/tokens/` | Backup tokens |

**Disable 2FA via CLI (SSH):**

```bash
# Local
python manage.py disable_2fa admin          # Disable for a specific user
python manage.py disable_2fa --all          # Disable for all users
python manage.py disable_2fa --list         # List users with 2FA enabled

# Docker
docker compose exec server python manage.py disable_2fa admin
```

### Daily Logs

The server automatically creates daily log files in `server/logs/YYYY-MM-DD/`:

- `server.log` — application log for the day
- `data_usage.log` — network RX/TX bytes recorded every hour

**Web access:** Logged-in admins can browse logs at `/admin-daily-logs/` (also available in the Admin dropdown menu). Sensitive data (tokens, passwords, secrets) is automatically masked in the web view. For `server.log`, a **Live** toggle button enables auto-refresh (polls every 3 seconds) to tail the log in real time.

**RPi streamer** also creates daily logs in `rpi_streamer/logs/YYYY-MM-DD/` (`streamer.log` + `data_usage.log`), accessible via SSH only.

### Camera Token File

The RPi streamer supports loading the camera token from a file instead of the YAML config:

```bash
(umask 077; cp rpi_streamer/configs/.token.example rpi_streamer/configs/.token)
# Replace the placeholder in configs/.token with your own camera token.
```

Only `*.example.yaml`, `config.example.json`, and `.token.example` are published. Runtime configurations, `.env` files, and real `.token` files are ignored by Git; keep deployment addresses and credentials in those local files.

This file is git-ignored. If `camera_token` is not set in the YAML config, the streamer reads `configs/.token` automatically.

### Admin URL

The Django admin panel is **not** at the default `/admin/` path for security. The default is `/control-panel/`. Set `DJANGO_ADMIN_PATH` in your local server `.env` to choose a different URL segment; never commit your private value.

### Running Tests

**Unit tests** (Django test framework — 2FA, logging, security):

```bash
cd server && source venv/bin/activate
DJANGO_SECRET_KEY='test-key' CAMERA_TOKEN='test' DJANGO_ALLOWED_HOSTS='*' DJANGO_DEBUG=1 \
  PYTHONPATH=.. python manage.py test tests.test_2fa tests.test_logging_security -v2
```

Covers: 2FA login flow (with/without OTP), OTP verification, wrong token rejection, rate limiting, `disable_2fa` CLI, admin URL obscurity, log sanitization, daily log creation, web log access auth, and path traversal protection.

**Acceptance tests** (quick connectivity and streaming smoke tests):

```bash
# Against local-dev (start server first: ./manage_server.sh local-dev)
python3 tests/acceptance_test.py --local             # All tests
python3 tests/acceptance_test.py --local connect     # Connection only

# Against production
TEST_CAMERA_TOKEN=<token> python3 tests/acceptance_test.py
```

Covers: WebSocket connection, camera auth, recording initialization, 100 MB / 1 GB transfers, sustained streaming with reconnects.

**Data integrity tests** (sent-vs-received byte verification via SSH — requires production server access):

```bash
# Install test dependencies
python3 -m venv tests/.venv && source tests/.venv/bin/activate
pip install websockets requests

# Run tests against production
TEST_CAMERA_TOKEN=<token> python3 tests/test_data_integrity.py          # All tests
TEST_CAMERA_TOKEN=<token> python3 tests/test_data_integrity.py quick    # Quick 50 MB
TEST_CAMERA_TOKEN=<token> python3 tests/test_data_integrity.py 1gb      # 1 GB transfer
```

Sends exact known data amounts via WebSocket, verifies recorded bytes on disk and in DB via SSH. Max acceptable difference: 2%. Tests: 50 MB, 100 MB (H.264 + JPEG), 500 MB, 1 GB, 5 GB, reconnect (5×60 MB), small/large frame sizes.

## Streamer Management

```bash
# From repo root
./manage_rpi_streamer.sh macos            # macOS webcam (JPEG)
./manage_rpi_streamer.sh macos_h264       # macOS webcam (H.264 via ffmpeg VideoToolbox)
./manage_rpi_streamer.sh rpi              # RPi Camera Module 3 (H.264)
./manage_rpi_streamer.sh rpi_ai           # RPi AI Camera (H.264)
./manage_rpi_streamer.sh synthetic        # Synthetic test pattern
./manage_rpi_streamer.sh --config path/to/config.yaml
./manage_rpi_streamer.sh setup-token      # Interactive token fetch from server
./manage_rpi_streamer.sh setup-venv       # Create venv + install requirements

# Auto-start on boot (systemd)
./manage_rpi_streamer.sh enable-startup rpi    # Enable auto-start
./manage_rpi_streamer.sh disable-startup       # Disable auto-start
./manage_rpi_streamer.sh startup-status        # Service status + logs
./manage_rpi_streamer.sh startup-logs [N]      # Last N log lines (default: 50)
```

---

## Docker Compose Files

| File | Networking | Use case |
|---|---|---|
| `docker-compose.yml` | `network_mode:host`, Nginx :80 | **Network mode** — LAN development (default) |
| `docker-compose.prod.yml` | Docker bridge (`proxynetwork`) + nginx-proxy :80/:443 | **Production** — VPS with SSL (camera.example.com) |

### Production Mode

Production uses `nginxproxy/nginx-proxy` + `acme-companion` for automatic SSL certificate provisioning via Let's Encrypt. All services run on a shared Docker bridge network (`proxynetwork`).

```
Internet → Cloudflare → nginx-proxy (443/SSL) → proxy-bridge (80) → daphne (8000)
WebRTC media: Browser ↔ daphne (direct UDP) or Browser ↔ coturn (TURN relay) ↔ daphne
```

| Container | Role |
|---|---|
| `nginx-proxy` | Reverse proxy on 80/443, auto-discovers backends via `VIRTUAL_HOST` |
| `nginx-acme` | Let's Encrypt certificate provisioning (ACME protocol) |
| `proxy-bridge` | Static files, rate limiting, WebSocket upgrade, forwards to daphne |
| `server` | Django/Daphne (port 8000, host network) |
| `redis` | Channel layer + pub/sub (port 6379, host network) |
| `coturn` | TURN relay (port 3478 UDP+TCP, relay range 49152–49252 UDP, host network). Required for viewers behind Symmetric NAT / CGNAT (e.g. Orange PL 5G) |

**First-time setup:**

```bash
./manage_server.sh init-prod            # Interactive: domain, email, TURN, generates secrets → server/.env
./manage_server.sh create-user          # Create user with 2FA (TOTP) enabled by default
./manage_server.sh delete-user          # Delete a user account
./manage_server.sh start prod      # Creates proxynetwork, pulls images, starts all services
```

### Server Environment Variables

Set via `server/.env` (generated by `./manage_server.sh init-prod`):

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | *(required)* | Django session signing key |
| `DJANGO_ALLOWED_HOSTS` | *(empty)* | Comma-separated allowed domains |
| `DJANGO_DEBUG` | `false` | Enable debug mode (`true`/`1`) |
| `CAMERA_TOKEN` | *(empty)* | Global fallback camera auth token |
| `RECORDINGS_DIR` | `/app/recordings` | Recording file storage path |
| `RECORDING_CHUNK_SIZE_MB` | `100` | New recording file every N MB |
| `STUN_SERVER` | *(empty)* | STUN server for NAT traversal |
| `TURN_SERVER` | *(empty)* | TURN relay URL |
| `TURN_USERNAME` | *(empty)* | TURN auth username |
| `TURN_PASSWORD` | *(empty)* | TURN auth password |
| `NETWORK_INTERFACE` | *(auto-detect)* | NIC for network stats (e.g. `eth0`, `enp1s0`). Auto-detects the physical interface, excluding Docker bridge/veth/loopback to avoid double-counting |
| `SSH_AUTH_LOG_PATH` | *(auto-detect)* | Path to SSH auth log. Auto-detects `/app/data/ssh_auth.log` (Docker) or `/var/log/auth.log` (local). See [SSH Auth Attempt Monitoring](#ssh-auth-attempt-monitoring) |
| `LOGIN_RATELIMIT_MAX_ATTEMPTS` | `5` | Failed login attempts before lockout |
| `LOGIN_RATELIMIT_WINDOW_SECONDS` | `300` | Lockout window (seconds) |
| `TZ` | `Europe/Warsaw` | Timezone for logs |

---

## WebSocket Protocol

### Camera → Server (`/ws/camera/feed/?camera_id=<id>`)

Authentication: `Authorization: Token <camera_token>` header (or `?token=` query param). Token is checked per-device first (Django Admin generated), then falls back to global `CAMERA_TOKEN` env var.

Each frame = two consecutive messages:

```
Text  → {"type":"metadata","encoding":"h264","current_fps":25,"keyframe":true,
          "bitrate_kbps":1842.5,"frame_count":312,"cpu_percent":18.3,
          "wifi_signal_dbm":-52,"voltage_v":4.98,"resolution":"1920x1080", ...}
Binary → <Annex B H.264 access unit bytes>
```

### Server → Viewer (`/ws/viewer/?camera_id=<id>`)

Authentication: Django session cookie (or `session_key` query param for Safari).

**Video delivery:** WebRTC (not WebSocket). The viewer WebSocket is used for:

```
Text  → JSON metadata (same object from camera, relayed via Redis)
Text  → {"type":"webrtc_answer","sdp":"..."} — SDP answer for WebRTC negotiation
Text  → {"type":"switched","camera_id":"..."} — Camera switch acknowledgement
```

**WebRTC signaling flow:**

1. Browser sends SDP offer via WS: `{"type":"webrtc_offer","sdp":"..."}`
2. Server creates `RTCPeerConnection`, subscribes to camera's `MediaRelay`
3. Server returns SDP answer via WS: `{"type":"webrtc_answer","sdp":"..."}`
4. ICE negotiation completes, video flows via RTP
5. Metadata continues flowing over WebSocket text messages

---

## Features

- **Multi-camera** — unlimited cameras, per-camera channel groups in Redis
- **Per-user camera access** — assign users to specific cameras via Django Admin (`allowed_users` M2M field with filter_horizontal widget); only assigned users can view/stream/download; cameras with no users assigned are hidden from everyone
- **Per-device token auth** — each camera gets its own token generated in Django Admin (Admin → Cameras → Generate token); global `CAMERA_TOKEN` env var still supported as fallback
- **WebRTC video delivery** — aiortc SFU with H.264 passthrough: raw NALUs forwarded directly as RTP packets, zero decode/re-encode on the server; ~25% CPU on RPi vs 313% with decode/encode
- **Instant viewer start** — latest metadata cached server-side, sent on WS connect; WebRTC track subscribed from MediaRelay; H.264 codec forced via `setCodecPreferences`
- **Dynamic FPS** — configurable idle FPS → active FPS on motion, software-gated (no sensor restart)
- **Idle keyframe forcing** — at low FPS, only IDR keyframes sent; `force_key_frame()` after each send prevents browser blackouts
- **Motion detection** — software frame differencing on lores stream with tunable threshold and cooldown
- **Camera rotation** — 0°/90°/180°/270°; `display_rotation` synced from streamer metadata to the server; applied as MP4 rotation metadata on recording playback and as CSS transform on live view
- **Yellow timestamp overlay** — burned into frame before encoding, visible in recordings
- **H.264 recording** — raw Annex B written to 100 MB chunked files; recordings remuxed to MP4 on first request with correct rotation
- **Recording management** — delete individual recordings or bulk "Delete All" with confirmation; download all recordings as sequential MP4 files
- **Recording retention** — configurable per-camera storage quota (`max_storage_gb`) in Django Admin; oldest recordings auto-pruned when exceeded; `prune_recordings` management command + hourly Docker task
- **Mobile-first UI** — recordings list uses card layout on mobile (≤640 px) with large touch targets; admin logs support text wrapping on iOS; all pages responsive
- **MP4 download** — playback page offers direct `.mp4` download with correct rotation; served as `Content-Disposition: attachment`
- **JPEG fallback** — JPEG-mode cameras deliver frames over WebSocket (base64) without WebRTC
- **INA219 power monitoring** — voltage, current, power, battery % in live stats
- **Stream diagnostics** — `stream_diagnostics.py` connects as a real WebRTC viewer (aiortc), analyzes frame timing, detects duplicates, gaps, frozen video, tracks WebRTC connection health and FPS transitions
- **Admin dashboard** — Stats page with CPU, memory, disk, 24H network I/O (physical interface only — auto-detects primary NIC, excludes Docker bridge/veth/loopback), active connections, SSH sessions; real-time charts with proper time-axis bounds for all time windows; daily log browser with live auto-refresh toggle for `server.log`; server log viewer with level filtering and polling; mobile-friendly
- **Live page stats** — send rate (MB/s, MB/m, MB/h), 24H rolling frame count and total MB sent (tracked server-side), bitrate, FPS, resolution, motion status, voltage, WiFi signal
- **Login rate limiting** — brute-force protection on the login page
- **YAML configuration** — one file per camera, no code changes needed

---

## Implementation Details

### H.264 Keyframe Management (Idle Mode)

**Problem:** At low FPS (1–5 fps), the browser decoder (jmuxer → MSE) needs a keyframe (IDR) to render. With `iperiod=15` at 25 fps sensor rate, natural keyframes occur every 600 ms. At 3 fps idle (333 ms intervals), every other frame the encoder produces is a P-frame — if sent, the browser has no reference and shows a black screen.

**Solution — Keyframes-only at idle FPS:**

1. **FPS gate** (`rpi_regular.py: get_encoded_chunks()`): When `target_fps < 20`, only keyframe access units (containing IDR NAL type 5) are forwarded. All P-frames are silently dropped.

2. **Force-after-send** (`rpi_regular.py: get_encoded_chunks()`): Immediately after sending an idle keyframe, `force_key_frame()` is called via V4L2 ioctl. This marks the *next* encoder output as IDR, guaranteeing a keyframe is ready before the next send interval (~333 ms at 3 fps).

3. **Reactive fallback**: If the next chunk at send time happens to be a P-frame (force didn't take effect in time due to encoder refractory period), the streamer forces a keyframe and waits for the next poll cycle (~50 ms). This ensures a keyframe is always available within one poll interval.

**Timing math (RPi-specific):**
- Sensor always at 25 fps → frame every 40 ms
- `force_key_frame()` latency: ~40 ms (encoder) + ~50 ms (poll) = ~90 ms
- At 3 fps idle, send interval = 333 ms → 243 ms margin after force
- At 1 fps idle, send interval = 1000 ms → 910 ms margin

These timings are RPi-side only — valid regardless of server location or network latency.

### FPS Gating (Software-Based)

The camera sensor always runs at its maximum frame rate (25 fps on RPi Camera Module 3). FPS changes are implemented as **software gating**, not sensor reconfiguration. This avoids:
- 1–3 second sensor restart latency on FPS changes
- Dropped frames during mode transitions
- Complex V4L2 reconfiguration

The `set_fps()` method simply updates `_target_fps`. The `get_encoded_chunks()` method checks elapsed time since last send: if `< 1/target_fps`, the frame is skipped.

At high FPS (≥ 20): All NAL types forwarded (IDR + P-frames).
At low FPS (< 20): Only IDR keyframes forwarded; P-frames dropped.

### Motion Detection Pipeline

1. **Dual-stream capture**: picamera2 configured with main stream (1920×1080 H.264) and lores stream (640×480 YUV420)
2. **Frame differencing**: `check_motion()` computes per-pixel absolute difference between consecutive lores frames
3. **Thresholding**: Pixels with delta > `motion_threshold` (default 25) are counted
4. **Trigger**: If changed pixels > `min_motion_area` (default 500), motion state = True
5. **Cooldown**: After motion ends, high FPS maintained for `detection_cooldown` (default 3 s)
6. **FPS transition**: Motion → `fps` (25); idle → `idle_fps` (configurable, default 1)

### FPS Transition Handling

On every FPS transition, `force_key_frame()` is called to ensure the first frame at the new rate is an IDR. This prevents "orphan P-frames" — P-frames sent without a preceding keyframe reference that cause browser decoder black screens.

**Idle → Active (e.g., 3 → 25 fps):**
- First keyframe arrives within ~90 ms (force latency)
- Transition is seamless — browser gets IDR immediately

**Active → Idle (e.g., 25 → 3 fps):**
- Keyframe forced, then FPS gate activates (P-frames dropped)
- First idle keyframe within ~72 ms typical

### WebRTC SFU (Server-Side Video Delivery)

**Previous approach:** WebSocket binary forwarding + jmuxer/MSE in browser (removed).

**Current approach:** aiortc-based Selective Forwarding Unit (SFU) with **H.264 passthrough** — the server never decodes or re-encodes video.

**How it works:**

1. **Ingest:** `CameraConsumer` receives raw Annex B H.264 from the streamer and calls `CameraVideoTrack.push_h264(data)`, buffering in an `asyncio.Queue(maxsize=30)`.

2. **Passthrough:** `CameraVideoTrack.recv()` wraps the raw H.264 bytes as `av.Packet` with wall-clock PTS (`(time.time() - start) * 90000`). Because it returns a `Packet` (not a `VideoFrame`), aiortc's `RTCRtpSender` uses the `pack()` path instead of `encode()` — which splits Annex B into NAL units and packetizes them into RTP payloads (STAP-A for small NALUs, FU-A fragmentation for large ones). **Zero CPU spent on decoding or encoding.**

3. **Codec enforcement:** `ViewerConsumer._handle_webrtc_offer()` calls `transceiver.setCodecPreferences()` with only H.264 codecs. This ensures the SDP negotiation selects H.264 (matching the camera's encoding) and prevents VP8/VP9 fallback that would require transcoding.

4. **Fan-out:** `MediaRelay` (from `aiortc.contrib.media`) subscribes one relay track per viewer. The relay passes `av.Packet` objects directly — no per-viewer decoding.

5. **Deliver:** Each viewer's `RTCPeerConnection` gets a relay track. aiortc handles RTP packetization, RTCP, DTLS/SRTP, and ICE. The browser receives native H.264 WebRTC video.

6. **Signaling:** SDP offer/answer exchanged over the existing viewer WebSocket — no separate signaling server needed. Browser sends `{type: "webrtc_offer", sdp: ...}`, server responds with `{type: "webrtc_answer", sdp: ...}`.

**Key files:**
- `webrtc.py` — `CameraVideoTrack` (H.264 → av.Packet passthrough), `CameraRelayTrack` (source + MediaRelay wrapper), global track/PC management
- `consumers.py` — `ViewerConsumer._handle_webrtc_offer()` creates per-viewer PeerConnection, forces H.264 codec, subscribes to relay, handles ICE gathering; periodic `group_add` refresh every 60s prevents Redis TTL expiry
- `live.html` — `RTCPeerConnection` with `recvonly` video transceiver, ICE gathering with 3s timeout, 5s debounce on `disconnected` state, auto-reconnect on `closed` state

**Performance (RPi 4, 1080p@25fps, H.264 passthrough):**

| Metric | Decode+Re-encode (old) | Passthrough (current) |
|---|---|---|
| Server CPU | 313% (2+ cores) | 25% |
| Delivered FPS | ~9 fps | ~25 fps (matches source) |
| Memory | 2.7 GB | ~200 MB |

**ICE/STUN:** By default, no external STUN server is used — WebRTC connects via host candidates only (works on LAN and VPS with a public IP). For VPS behind NAT (AWS/GCP/Azure), set `STUN_SERVER` env var or use `--stun` flag: `./manage.sh start prod --stun stun:stun.l.google.com:19302`. A TURN server may also be needed for symmetric NAT.

**Periodic disconnect fix (group_expiry):** Django Channels Redis layer has a `group_expiry` TTL for channel group membership. The default (120 s) caused browsers to stop receiving metadata after ~2 minutes, which triggered the WS activity timeout (30 s) and cascaded into a WebRTC disconnect (~2.5 min total). Fixed by:
- `group_expiry: 86400` (24 h) in `settings.py`
- Both `CameraConsumer` and `ViewerConsumer` call `group_add` every 60 s as a safety net

**Docker limitation:** Docker Desktop macOS does not forward UDP ports. WebRTC requires UDP for RTP/ICE. Solutions:
- Run daphne on the host (recommended for macOS dev — see `setup_venv.sh`)
- Use `network_mode: host` on Linux Docker (see `docker-compose.yml`)
- On production Linux servers, `network_mode: host` is recommended for WebRTC



### Per-Device Camera Tokens

Each camera can have its own authentication token, managed entirely in **Django Admin → Cameras**:

1. Open Django Admin (`/control-panel/`) → **Cameras**
2. Select one or more camera rows
3. **Actions** → **Generate new auth token** → **Go**
4. Copy the token from the `Token` column and paste into `rpi_streamer/configs/rpi-cam.yaml` as `camera_token`

**Authentication flow in `middleware.py`:**
1. Extract token from `Authorization: Token <token>` header (or `?token=` query param)
2. Look up `Camera` by `camera_id` query param; compare token with `hmac.compare_digest` (constant-time)
3. If not matched: fall back to global `CAMERA_TOKEN` env var (backward compatibility)
4. `scope["camera_authenticated"] = True` only if either check passes

This means you can run a mixed fleet with per-device tokens (secure, rotatable per camera) and legacy cameras using the global token simultaneously.

### Recording Retention

Each camera has a configurable **max storage quota** (`max_storage_gb`, default 10 GB) set in Django Admin → Cameras:

- **Auto-prune on recording close:** When a recording chunk finishes, `_prune_old_recordings()` is called. If `storage_used_bytes > max_bytes`, it deletes the oldest recordings (by `started_at`) until within quota. Both `.h264` raw files and any cached `.mp4` are removed.
- **Hourly background prune:** Docker `CMD` launches a background loop (`while true; do sleep 3600; python manage.py prune_recordings; done`) as an additional safety net.
- **Manual prune:** `./manage_server.sh shell` → `python manage.py prune_recordings`
- **Storage visible in admin:** The `storage_used_gb` property is displayed in the Camera list view alongside the quota.

### Network Stats Monitoring

The admin dashboard tracks CPU, memory, and network I/O in 30-second intervals via a background thread. Stats are persisted in the `NetworkHourlyStat` model (hourly buckets) for historical charts.

**Physical interface only:** Network stats use `psutil.net_io_counters(pernic=True)` filtered to the primary physical NIC. This avoids the Docker bridge amplification problem where the same data traverses `enp1s0` → `br-*` → `veth*`, causing each byte to be counted 3–5× in aggregate counters. The interface is auto-detected (highest `bytes_recv`, excluding `lo`, `docker*`, `br-*`, `veth*`, `virbr*`, `flannel*`, `cni*`, `cali*`) or set manually via the `NETWORK_INTERFACE` env var.

### SSH Auth Attempt Monitoring

The admin dashboard shows SSH authentication attempts (accepted & failed) parsed from the system auth log. Since the Docker container should **not** run with access to `/var/log/auth.log`, a host-side cron script extracts the relevant entries.

**Setup (one-time, on the server):**

```bash
./manage_server.sh init-cron
# Interactive — installs a root crontab entry (runs every minute).
# Extracts sshd entries to data/ssh_auth.log for the dashboard.
```

**How it works:**
1. `extract_ssh_auth.sh` runs as root via cron (every minute)
2. Extracts `Accepted`, `Failed`, and `Invalid user` sshd lines from auth.log
3. Writes to `data/ssh_auth.log` (readable by the container via `./data:/app/data`)
4. The Django view reads and caches entries (30s TTL) — no direct auth.log access needed

### MP4 Rotation Fix

The RPi camera is physically mounted at an angle (e.g., 270°). libcamera only applies hardware transform for 180° rotations; 90° and 270° are handled by swapping capture dimensions and relying on the viewer to rotate.

**Live view:** CSS `transform: rotate(Xdeg)` applied using `display_rotation` from streamer metadata.

**Recordings:** The `display_rotation` value is synced from streamer metadata to the `Camera.display_rotation` model field whenever a metadata message arrives. When the `.mp4` is built via ffmpeg remux, the rotation is written as MP4 container metadata:

```bash
ffmpeg -r <fps> -i recording.h264 -c copy -movflags +faststart \
  -metadata:s:v rotate=270 recording.mp4
```

This is read by all standard players (VLC, QuickTime, iOS, Android) and rotates display without re-encoding. Any previously cached `.mp4` without rotation must be deleted (or will be regenerated on next request once `display_rotation` is populated).

### Stream Diagnostics Tool

`tests/stream_diagnostics.py` connects as a **real WebRTC viewer** using aiortc (same path as the browser), receives decoded video frames, and analyzes stream health.

**Architecture:**
1. HTTP login → session cookie
2. WebSocket → signaling (SDP offer/answer) + metadata
3. WebRTC (aiortc) → video frames (decoded by aiortc → numpy array)

**Detected glitch types:**
| Type | Detection method |
|---|---|
| `duplicate` | Exact MD5 hash match (same encoded frame re-sent) |
| `gap` | Frame interval > 3× expected (based on reported FPS) |
| `frozen` | Near-identical frames (SSIM > 0.9999) for > 2.0 s |
| `corrupt` | Frame decode failure (to_ndarray error) |
| `dimension_change` | Frame resolution changed mid-stream |
| `disconnect` | WebRTC connection state → disconnected/failed/closed |

**WebRTC connection health tracking:**
- Time to first frame (ms)
- ICE failures, disconnects, reconnects
- Connected vs disconnected time percentage

**FPS transition analysis:**
- Measures latency to first frame after each FPS change
- Counts frames delivered per transition window
- Reports per-transition health with direction (UP/DOWN)

**Usage:**
```bash
cd tests
source venv/bin/activate
python stream_diagnostics.py --host 192.0.2.10 -u admin -p admin --camera rpi-cam --duration 300 -v
```

**Example output (5-minute test on RPi, H.264 passthrough):**
```
  Duration           : 300.4s
  Total frames (RTP) : 7344
  Average FPS        : 24.45
  Disconnects        : 0
  Glitches           : 4 minor gaps
  Verdict: WARN - MINOR GLITCHES DETECTED
```

### Deployment Architecture

The system supports deployment on any network topology:

```
┌─────────────┐     WebSocket      ┌──────────────────┐                    ┌─────────┐
│ RPi Streamer │ ─────────────────→ │  Server (Docker)  │ ←── WS signaling ─ │ Browser │
│  H.264 src   │   binary + JSON    │  Nginx → Daphne   │ ── WebRTC RTP ──→ │  <video> │
└─────────────┘                     │  aiortc SFU       │ ── WS metadata ─→ │  stats  │
                                    │  Redis · SQLite    │                   └─────────┘
                                    └──────────────────┘
```

**Production (VPS with SSL):**

```
┌─────────────┐  WSS   ┌────────────┐  HTTPS  ┌──────────────┐      ┌───────────────┐      ┌────────┐
│ RPi Streamer │ ─────→ │ Cloudflare │ ──────→ │ nginx-proxy  │ ───→ │ proxy-bridge  │ ───→ │ daphne │
└─────────────┘         └────────────┘  :443   │ + acme (SSL) │ :80  │ (rate limit)  │ :8000│ Django │
                                               └──────────────┘      └───────────────┘      └────────┘

WebRTC media (SRTP/UDP):
┌─────────┐  direct UDP   ┌──────────────────┐
│ Browser  │ ←───────────→ │ daphne (aiortc)  │   ← works on most networks
└─────────┘               └──────────────────┘

┌─────────┐  UDP/TCP  ┌─────────┐  relay  ┌──────────────────┐
│ Browser  │ ────────→ │ coturn  │ ──────→ │ daphne (aiortc)  │   ← fallback for restrictive NATs
└─────────┘   :3478   │  TURN   │         └──────────────────┘       (e.g. Orange PL 5G / CGNAT)
                      └─────────┘
```

**Server can run on:**
- Same RPi as streamer (localhost)
- Separate RPi on LAN (e.g., 192.168.1.x)
- VPS / cloud server (any reachable address)

All H.264 timing calculations (keyframe forcing, FPS gating) happen on the streamer side. The server forwards H.264 directly via WebRTC (aiortc SFU passthrough) — no decode/re-encode. Network latency between streamer and server does not affect keyframe timing. WebRTC handles its own congestion control and jitter buffering.

---

## Security

### Hardening Applied

| Area | Measure |
|---|---|
| **SSL/TLS** | Let's Encrypt via acme-companion; TLSv1.2+; HSTS preload (1 year) |
| **Django** | `SECURE_SSL_REDIRECT`, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS` |
| **Cookies** | `Secure`, `HttpOnly`, `SameSite=Lax` on both session and CSRF cookies |
| **Passwords** | Enforced validators: min 12 chars, no common passwords, no numeric-only |
| **Headers** | `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` |
| **Rate limiting** | Nginx: `/login/` 5r/m, `/api/` 30r/m, general 10r/s; Django: per-IP login attempt tracking |
| **Container** | Runs as non-root `appuser`; `no-new-privileges` security option |
| **Open redirect** | `next` parameter validated via `url_has_allowed_host_and_scheme()` |
| **Auth** | Token auth with `hmac.compare_digest` (timing-safe); per-device tokens in Django Admin; per-user camera access via `allowed_users` M2M |
| **Recording paths** | Filename pattern validation + `resolve()` path traversal checks |
| **Secrets** | `.env` file git-ignored; never printed to terminal; auto-generated by `init` |
| **Admin URL** | Configurable via local `DJANGO_ADMIN_PATH`; default `/control-panel/` |
| **2FA** | TOTP-based two-factor on admin panel (Google Authenticator / Apple Passwords) |
| **TURN relay** | coturn with long-term credentials (`lt-cred-mech`); auth required for allocation; relay port range restricted (49152–49252); no multicast peers; credentials only served to authenticated viewers via `@login_required` view |

### Known Limitations

- SQLite in production (adequate for single-server, low-concurrency use)
- Docker socket mounted in nginx-proxy and acme-companion (required for container auto-discovery)
- WebSocket session key fallback for Safari (short-lived, session-scoped)
- No Content Security Policy header (would require `unsafe-inline` for current templates)
