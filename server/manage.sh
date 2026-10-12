#!/usr/bin/env bash
# Server Management Script
# Usage: ./manage.sh [command] [args...]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

show_help() {
    cat << EOF
Server Management Script

Usage: ./manage.sh [command] [options]

Commands:
  start [mode]      Start server via Docker
  stop              Stop Docker server
  restart [mode]    Restart server
  rebuild [mode]    Rebuild and restart server
  status            Show server status (containers, ports)
  logs [service]    Show logs (server, redis, nginx, proxy-bridge, nginx-proxy, acme)
  shell             Open Django shell
  import-recordings Import recordings from disk into DB
  nuke              DANGER: Remove all Docker data (preserves SSL certs)
  nuke --include-certs  Also remove SSL certificate volumes

Use ./manage_server.sh (root wrapper) for:
  init-prod         Interactive .env setup for production
  create-user       Create a user with 2FA (TOTP) enabled by default
  local-dev         Start local dev server (no Docker)

Modes:
  network           WebRTC + nginx on port 80, network_mode:host (default)
                    LAN development with auto-created admin user.
  prod              Production behind reverse proxy (camera.example.com)
                    Requires .env with DJANGO_SECRET_KEY. Uses proxynetwork.

Options:
  --stun <url>      Set STUN server for NAT traversal (default: none)
                    Example: --stun stun:stun.l.google.com:19302

Examples:
  ./manage.sh start                  # Start network mode (LAN, port 80)
  ./manage.sh start network          # Same as above
  ./manage.sh start prod             # Start production mode (VPS + proxy)
  ./manage.sh start prod --stun stun:stun.l.google.com:19302
  ./manage.sh stop                   # Stop server
  ./manage.sh logs server            # Show server logs
  ./manage.sh rebuild                # Rebuild network mode
  ./manage.sh rebuild prod           # Rebuild production mode
  ./manage.sh nuke                   # Clean all Docker data
  ./manage.sh shell                  # Open Django shell
EOF
}

get_compose_file() {
    local mode="${1:-network}"
    case "$mode" in
        network)
            echo "docker-compose.yml"
            ;;
        prod)
            echo "docker-compose.prod.yml"
            ;;
        *)
            echo "Error: Unknown mode '$mode'. Use: network or prod" >&2
            exit 1
            ;;
    esac
}

cmd_start() {
    local mode="${1:-network}"
    local compose_file=$(get_compose_file "$mode")
    
    echo "🚀 Starting server (mode: $mode)"
    
    if [ -n "$STUN_SERVER" ]; then
        echo "🌐 STUN server: $STUN_SERVER"
    else
        echo "🔒 No STUN server (LAN/direct IP only — no external connections)"
    fi
    
    if [ "$mode" = "prod" ]; then
        # Ensure proxynetwork exists
        docker network inspect proxynetwork >/dev/null 2>&1 || {
            echo "⚠️  Creating proxynetwork (docker network create proxynetwork)"
            docker network create proxynetwork
        }
        # Check for required env vars
        if [ -z "${DJANGO_SECRET_KEY:-}" ] && ! grep -q DJANGO_SECRET_KEY .env 2>/dev/null; then
            echo "❌ Production mode requires DJANGO_SECRET_KEY."
            echo "   Create .env file or export DJANGO_SECRET_KEY=<your-key>"
            echo "   Generate one: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
            exit 1
        fi
        # Allow bridge→host traffic for server (host networking) reachability
        # Server uses network_mode:host for WebRTC UDP; proxy-bridge on bridge needs to reach it
        docker run --rm --net=host --privileged alpine sh -c \
            "apk add -q iptables 2>/dev/null; \
             iptables -C INPUT -i br-+ -p tcp --dport 8000 -j ACCEPT 2>/dev/null || iptables -I INPUT -i br-+ -p tcp --dport 8000 -j ACCEPT; \
             iptables -C INPUT -p udp --dport 32768:60999 -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport 32768:60999 -j ACCEPT" \
            2>/dev/null && echo "🔧 iptables: bridge→host + WebRTC UDP configured" || true
    fi
    
    docker compose -f $compose_file up -d
    
    # Auto-create admin user for network mode (development)
    if [ "$mode" = "network" ]; then
        echo "🔑 Checking for admin user..."
        sleep 3
        
        docker compose -f $compose_file exec -T server python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('✅ Created admin user (username: admin, password: admin)')
else:
    print('ℹ️  Admin user already exists')
" 2>/dev/null || echo "⚠️  Could not create admin user (server may still be starting)"
    fi
    
    echo "✅ Server started"
    echo ""
    docker compose -f $compose_file ps
    echo ""
    if [ "$mode" = "network" ]; then
        echo "🌐 Nginx on port 80 — http://<your-ip>"
    elif [ "$mode" = "prod" ]; then
        echo "🌐 Production — https://${DOMAIN:-camera.example.com} (via reverse proxy)"
    fi
    echo "To view logs: ./manage.sh logs"
    echo "To stop: ./manage.sh stop"
}

cmd_stop() {
    echo "🛑 Stopping server..."
    
    for compose_file in docker-compose*.yml; do
        if docker compose -f "$compose_file" ps -q 2>/dev/null | grep -q .; then
            echo "Stopping $compose_file..."
            docker compose -f "$compose_file" down
        fi
    done
    
    echo "✅ Server stopped"
}

cmd_status() {
    echo "📊 Server Status:"
    echo "================"
    
    local found=0
    for compose_file in docker-compose*.yml; do
        if docker compose -f "$compose_file" ps -q 2>/dev/null | grep -q .; then
            echo ""
            echo "Running from: $compose_file"
            docker compose -f "$compose_file" ps
            found=1
            break
        fi
    done
    
    if [ $found -eq 0 ]; then
        echo "❌ No server containers running"
        return 1
    fi
    
    echo ""
    echo "Port bindings:"
    docker ps --format "table {{.Names}}\t{{.Ports}}" | grep -E "server|nginx|redis" || true
}

cmd_logs() {
    local service="${1:-}"
    
    for compose_file in docker-compose*.yml; do
        if docker compose -f "$compose_file" ps -q 2>/dev/null | grep -q .; then
            echo "📋 Logs from $compose_file:"
            echo "================================"
            if [ -n "$service" ]; then
                docker compose -f "$compose_file" logs --tail=100 -f "$service"
            else
                docker compose -f "$compose_file" logs --tail=100 -f
            fi
            return 0
        fi
    done
    
    echo "No running services found."
    exit 1
}

cmd_restart() {
    local mode="${1:-network}"
    echo "🔄 Restarting server..."
    cmd_stop
    sleep 2
    cmd_start "$mode"
}

cmd_rebuild() {
    local mode="${1:-network}"
    local compose_file=$(get_compose_file "$mode")
    
    echo "🔨 Rebuilding and restarting server..."
    
    docker compose -f $compose_file down
    docker compose -f $compose_file up --build -d
    
    # Auto-create admin user for network mode
    if [ "$mode" = "network" ]; then
        echo "🔑 Checking for admin user..."
        sleep 3
        
        docker compose -f $compose_file exec -T server python manage.py shell -c "
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='admin').exists():
    User.objects.create_superuser('admin', 'admin@example.com', 'admin')
    print('✅ Created admin user (username: admin, password: admin)')
else:
    print('ℹ️  Admin user already exists')
" 2>/dev/null || echo "⚠️  Could not create admin user (server may still be starting)"
    fi
    
    echo "✅ Server rebuilt and started"
    echo ""
    docker compose -f $compose_file ps
}

cmd_nuke() {
    echo "🧨 DANGER: This will remove ALL Docker data!"
    echo "   - All containers (running and stopped)"
    echo "   - All images"
    echo "   - All volumes (except SSL certs/acme)"
    echo "   - All networks (except defaults)"
    echo ""
    echo "   SSL certificates are preserved to avoid Let's Encrypt rate limits."
    echo "   Use --include-certs to also remove certificates."
    echo ""
    read -p "Are you sure? Type 'YES' to continue: " confirm
    
    if [ "$confirm" != "YES" ]; then
        echo "Aborted."
        exit 0
    fi
    
    local include_certs=false
    if [ "${1:-}" = "--include-certs" ]; then
        include_certs=true
        echo "⚠️  Including SSL certificates in nuke!"
    fi

    echo ""
    echo "Nuking Docker..."
    
    echo "→ Stopping containers..."
    docker stop $(docker ps -aq) 2>/dev/null || true
    
    echo "→ Removing containers..."
    docker rm -f $(docker ps -aq) 2>/dev/null || true
    
    echo "→ Removing images..."
    docker rmi -f $(docker images -q) 2>/dev/null || true
    
    echo "→ Removing volumes..."
    # Preserve cert/acme volumes to avoid Let's Encrypt rate limits (5 certs/week)
    local protected_volumes="server_certs server_acme"
    if [ "$include_certs" = "true" ]; then
        docker volume rm $(docker volume ls -q) 2>/dev/null || true
    else
        for vol in $(docker volume ls -q 2>/dev/null); do
            local skip=false
            for pv in $protected_volumes; do
                if [ "$vol" = "$pv" ]; then
                    skip=true
                    break
                fi
            done
            if [ "$skip" = "true" ]; then
                echo "   ⏭  Keeping volume: $vol (SSL certs)"
            else
                docker volume rm "$vol" 2>/dev/null || true
            fi
        done
    fi
    
    echo "→ Pruning networks..."
    docker network prune -f 2>/dev/null || true
    
    echo "→ System prune..."
    docker system prune -af 2>/dev/null || true
    
    echo ""
    echo "✅ Docker nuked successfully"
    if [ "$include_certs" = "false" ]; then
        echo "   SSL cert volumes preserved (use --include-certs to remove)"
    fi
}

cmd_shell() {
    for compose_file in docker-compose*.yml; do
        if docker compose -f "$compose_file" ps -q server 2>/dev/null | grep -q .; then
            echo "🐚 Opening Django shell..."
            docker compose -f "$compose_file" exec server python manage.py shell
            return 0
        fi
    done
    
    echo "Error: Server container not running. Start it first with: ./manage.sh start"
    exit 1
}

cmd_import_recordings() {
    for compose_file in docker-compose*.yml; do
        if docker compose -f "$compose_file" ps -q server 2>/dev/null | grep -q .; then
            echo "📥 Importing recordings..."
            docker compose -f "$compose_file" exec server python manage.py shell -c "
from surv_server.stream.models import Camera, Recording
from django.conf import settings
import os, struct

recordings_dir = settings.RECORDINGS_DIR
count = 0
for camera_dir in os.listdir(recordings_dir):
    cam_path = os.path.join(recordings_dir, camera_dir)
    if not os.path.isdir(cam_path): continue
    camera, _ = Camera.objects.get_or_create(camera_id=camera_dir)
    for f in sorted(os.listdir(cam_path)):
        if not f.endswith('.mjpeg'): continue
        filepath = os.path.join(cam_path, f)
        frames = 0
        with open(filepath, 'rb') as fh:
            while True:
                hdr = fh.read(4)
                if len(hdr) < 4: break
                sz = struct.unpack('>I', hdr)[0]
                if sz == 0 or sz > 50_000_000: break
                fh.seek(sz, 1)
                frames += 1
        size = os.path.getsize(filepath)
        _, created = Recording.objects.get_or_create(
            camera=camera, filename=f,
            defaults={'frame_count': frames, 'size_bytes': size}
        )
        if created: count += 1
print(f'Imported {count} new recordings')
"
            return 0
        fi
    done
    
    echo "Error: Server container not running. Start it first with: ./manage.sh start"
    exit 1
}

# Main command router
CMD="${1:-}"
shift 2>/dev/null || true

# Parse remaining args
MODE=""
STUN_SERVER="${STUN_SERVER:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --stun)
            STUN_SERVER="${2:-}"
            shift 2
            ;;
        *)
            if [ -z "$MODE" ]; then
                MODE="$1"
            fi
            shift
            ;;
    esac
done

export STUN_SERVER

case "$CMD" in
    start)
        cmd_start "${MODE:-network}"
        ;;
    stop)
        cmd_stop
        ;;
    restart)
        cmd_restart "${MODE:-network}"
        ;;
    rebuild)
        cmd_rebuild "${MODE:-network}"
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs "${MODE:-}"
        ;;
    nuke)
        cmd_nuke "${MODE:-}"
        ;;
    shell)
        cmd_shell
        ;;
    import-recordings)
        cmd_import_recordings
        ;;
    help|--help|-h)
        show_help
        ;;
    "")
        echo "Error: No command specified"
        echo ""
        show_help
        exit 1
        ;;
    *)
        echo "Error: Unknown command: $CMD"
        echo ""
        show_help
        exit 1
        ;;
esac
