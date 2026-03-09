# Server Management Script

Simple script to manage the Django server via Docker Compose.

## Quick Start

```bash
cd server

# Start server (network mode — LAN, port 80)
./manage.sh start

# Admin user auto-created: admin / admin
# Access at: http://<your-ip>

# Check status
./manage.sh status

# View logs
./manage.sh logs

# Stop server
./manage.sh stop
```

## Commands

### Init (Production Setup)
```bash
# Run from project root
./manage_server.sh init-prod
```
Interactive wizard that creates `server/.env` with all required production secrets:
- **Domain** — your production domain (default: `<domain>`)
- **ACME email** — email for Let's Encrypt certificate registration via acme-companion
- **DJANGO_SECRET_KEY** — auto-generated 50-char random key
- **CAMERA_TOKEN** — auto-generated URL-safe token for camera authentication
- **STUN_SERVER** — optional STUN server for NAT traversal
- **TURN_SERVER** — TURN relay URL (auto-detected from public IP, or enter manually)
- **TURN_USERNAME** — TURN credential username (default: `camrelay`)
- **TURN_PASSWORD** — auto-generated 32-char random password

If `.env` already exists, you'll be asked before overwriting.

After running, open firewall ports for TURN if needed:
```bash
sudo ufw allow 3478/udp && sudo ufw allow 3478/tcp && sudo ufw allow 49152:49252/udp
```

### Create User (with 2FA)
```bash
# Run from project root (server must be running)
./manage_server.sh create-user
```
Interactive wizard that creates a user account with **2FA (TOTP) enabled by default**. The TOTP device is immediately active — the user must scan the QR code before their first login.

Password entry shows `*` per character (masked but visible count).

**Interactive mode** (prompts for all fields):
```bash
./manage_server.sh create-user
# Username: alice
# Password: ********
# Confirm password: ********
# Email (optional):
```

**Non-interactive / scripted** (all flags):
```bash
./manage_server.sh create-user alice --password secret --email alice@example.com
./manage_server.sh create-user admin --superuser           # superuser account
./manage_server.sh create-user bob --password secret --no-qr  # skip QR output
```

After creation the command prints:
- ASCII QR code in the terminal — scan with **Google Authenticator**, **Apple Passwords**, or **Authy**
- Provisioning URI for manual entry in any TOTP app

> ⚠️ Save the QR code or provisioning URI immediately — it cannot be retrieved later. If lost, use `./manage_server.sh create-user` to create a new account or `python manage.py disable_2fa <username>` to reset 2FA on an existing one.

### Delete User
```bash
# Run from project root (server must be running)
./manage_server.sh delete-user
```
Deletes a user account and all associated data (2FA devices, sessions).

**Interactive mode** (prompts for username and confirmation):
```bash
./manage_server.sh delete-user
# Username to delete: alice
# ⚠️  This will permanently delete user 'alice' and all associated data.
# Type the username to confirm: alice
```

**Non-interactive / scripted** (with `--force` to skip confirmation):
```bash
./manage_server.sh delete-user alice             # asks for confirmation
./manage_server.sh delete-user alice --force     # skip confirmation
```

### Start Server
```bash
./manage.sh start [mode]
```
Modes:
- `network` (default) — WebRTC + nginx on port 80, `network_mode:host`. LAN development.
- `prod` — Production behind reverse proxy (<domain>). Requires `.env` with `DJANGO_SECRET_KEY`.

**Auto-creates admin user in network mode:**
- Username: `admin`
- Password: `admin`

Examples:
```bash
./manage.sh start              # Network mode (auto-creates admin)
./manage.sh start network      # Same as above
./manage.sh start prod         # Production mode (no auto-creation)
```

### Stop Server
```bash
./manage.sh stop
```
Stops all running Docker Compose services.

### Restart Server
```bash
./manage.sh restart [mode]
```
Stops and starts the server.

### Rebuild Server
```bash
./manage.sh rebuild [mode]
```
Rebuilds Docker image and restarts (use after code changes).

### Check Status
```bash
./manage.sh status
```
Shows running containers and port bindings.

### View Logs
```bash
./manage.sh logs [service]
```
Examples:
```bash
./manage.sh logs               # All services
./manage.sh logs server        # Server only
./manage.sh logs redis         # Redis only
```

### Django Shell
```bash
./manage.sh shell
```
Opens Django shell in the running container.

### Import Recordings
```bash
./manage.sh import-recordings
```
Scans `/recordings` volume and imports files into database.

### Nuke Docker
```bash
./manage.sh nuke                # Preserves SSL cert volumes
./manage.sh nuke --include-certs  # Also removes cert volumes
```
⚠️ **DANGER**: Removes all Docker containers, images, volumes, and networks. Requires typing `YES` to confirm.

By default, SSL certificate volumes (`server_certs`, `server_acme`) are **preserved** to avoid Let's Encrypt rate limits (max 5 certificates per 168 hours). Use `--include-certs` to remove them too.

## Options

### STUN Server
By default, no external STUN server is used (no outbound connections). For VPS behind NAT (AWS/GCP/Azure), pass a STUN server:
```bash
./manage.sh start prod --stun stun:stun.l.google.com:19302
```

## Modes in Detail

### Network Mode (default)
- `network_mode:host` — server, redis, and nginx all use host networking
- nginx on port 80, daphne on port 8000
- WebRTC UDP works natively via host network
- Auto-creates admin user

### Production Mode
- All services on Docker bridge network (`proxynetwork`) — no `network_mode:host`
- `nginx-proxy` (nginxproxy/nginx-proxy) on ports 80/443 — auto-discovers containers via `VIRTUAL_HOST`
- `acme-companion` (nginxproxy/acme-companion) — auto-provisions Let's Encrypt SSL certificates
- `proxy-bridge` serves static files + handles WebSocket upgrades in front of daphne
- Daily logs persisted in `logs_data` volume (`/app/logs/YYYY-MM-DD/`)
- Requires `.env` (generate with `./manage_server.sh init-prod`):
  - `DJANGO_SECRET_KEY` — Django secret key (auto-generated)
  - `CAMERA_TOKEN` — camera authentication token (auto-generated)
  - `DOMAIN` — production domain
  - `ACME_EMAIL` — email for Let's Encrypt registration
  - `STUN_SERVER` — optional STUN server for NAT traversal
  - `TURN_SERVER` — TURN relay URL (auto-detected or manual, e.g. `turn:<server-ip>:3478?transport=udp`)
  - `TURN_USERNAME` — TURN long-term credential username (default: `camrelay`)
  - `TURN_PASSWORD` — TURN long-term credential password (auto-generated)

**Traffic flow:**
```
Internet → Cloudflare → nginx-proxy (443/SSL) → proxy-bridge (80) → daphne (8000)
WebRTC media: Browser ↔ daphne (direct UDP) or Browser ↔ coturn ↔ daphne (TURN relay)
All containers communicate via Docker DNS on proxynetwork.
```

#### Production Setup
```bash
# 1. Generate .env interactively
./manage_server.sh init-prod

# 2. Start (creates proxynetwork, pulls images, starts all services)
./manage_server.sh start prod
```

#### Production Services
| Container | Role |
|---|---|
| `nginx-proxy` | Reverse proxy on 80/443, auto-discovers backends |
| `nginx-acme` | Let's Encrypt cert provisioning via ACME protocol |
| `proxy-bridge` | Bridges proxynetwork ↔ host network (daphne:8000) |
| `server` | Django/Daphne on host network (port 8000) |
| `redis` | Redis on host network (port 6379) |
| `coturn` | TURN relay on host network (port 3478 UDP+TCP, relay 49152–49252 UDP). Required for viewers behind Symmetric NAT / CGNAT (e.g. Orange PL 5G) |

## Typical Workflows

### Daily Development (LAN)
```bash
./manage.sh start
# Make code changes...
./manage.sh rebuild
./manage.sh logs server
```

### Production Deployment (VPS)
```bash
./manage.sh start prod
./manage.sh logs server
./manage.sh logs proxy-bridge
```

### Clean Slate
```bash
./manage.sh nuke
./manage.sh start
./manage.sh import-recordings
```

## Troubleshooting

**Server won't start?**
```bash
./manage.sh logs
```

**Port 80 already in use?**
```bash
./manage.sh stop
lsof -i :80
```

**Production: proxy-bridge can't reach server?**
```bash
# Check proxynetwork exists
docker network inspect proxynetwork
# Test from inside proxy-bridge
docker exec server-proxy-bridge-1 wget -qO /dev/null -S http://server:8000/login/
# Check nginx-proxy generated config
docker exec nginx-proxy cat /etc/nginx/conf.d/default.conf
```

**SSL certificate not provisioning?**
```bash
# Check acme-companion logs
docker logs nginx-acme
# Verify LETSENCRYPT_HOST is set
docker inspect server-proxy-bridge-1 | grep LETSENCRYPT
```

**SSL handshake failed after nuke?**

Let's Encrypt enforces rate limits: max 5 certificates per 168 hours per domain. If you nuked cert volumes, you may need to wait up to 7 days. Workaround: generate a self-signed cert via the acme container (Cloudflare "Full" mode accepts it):
```bash
docker exec nginx-acme openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout /etc/nginx/certs/<DOMAIN>.key \
  -out /etc/nginx/certs/<DOMAIN>.crt \
  -subj "/CN=<DOMAIN>"
docker restart server-proxy-bridge-1   # triggers nginx-proxy config regen
```

## Security Hardening

The production setup includes multiple layers of security:

### Nginx Rate Limiting
- `/login/` — 5 requests/minute (brute-force protection)
- `/api/` — 30 requests/minute
- General — 10 requests/second
- WebSocket — 10r/s with burst of 5

### Security Headers (proxy-bridge)
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: same-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- `server_tokens off` (no nginx version disclosure)

### Django Security (production mode)
- `SECURE_SSL_REDIRECT = True`
- `SECURE_PROXY_SSL_HEADER` configured for proxy chain
- `SECURE_HSTS_SECONDS = 31536000` (1 year) with preload
- `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` enabled
- `CSRF_TRUSTED_ORIGINS` auto-configured from domain
- `CSRF_COOKIE_SAMESITE = Lax`
- Password validators enforced (min 12 chars)

### Container Security
- Server runs as non-root user (`appuser`)
- `no-new-privileges` security option on server container
- `client_max_body_size 50M` (reduced from 200M)

### Open Redirect Prevention
- `next` parameter validated via `url_has_allowed_host_and_scheme()`
