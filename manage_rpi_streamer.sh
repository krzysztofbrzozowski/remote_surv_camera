#!/usr/bin/env bash
# RPi Streamer Management Script
# Usage: ./manage_rpi_streamer.sh [camera_source] [options]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAMER_DIR="$SCRIPT_DIR/rpi_streamer"

SERVICE_NAME="rpi-streamer"
VENV_DIR="$SCRIPT_DIR/venv"
LOG_DIR="$STREAMER_DIR/logs"

show_help() {
    cat << EOF
RPi Camera Streamer Management Script

Usage: ./manage_rpi_streamer.sh [command] [options]

Camera Sources (run directly):
  macos         macOS/USB camera with motion detection
  rpi           Raspberry Pi regular camera (Camera Module 3) with motion detection
  rpi_ai        Raspberry Pi AI Camera (IMX500) with hardware detection
  synthetic     Synthetic test pattern (no camera required)

Options:
  --config FILE Use custom config file (overrides camera source)

Setup & Deployment:
  setup-token                Fetch camera token from server API and save to config
  setup-venv                 Create Python venv and install requirements

Auto-Start (systemd):
  enable-startup <source>    Enable auto-start on boot (e.g. enable-startup rpi)
  disable-startup            Disable and remove auto-start service
  startup-status             Show service status and recent logs
  startup-logs [N]           Show last N lines of service logs (default: 50)

Examples:
  ./manage_rpi_streamer.sh macos                    # Run with macOS camera
  ./manage_rpi_streamer.sh rpi                      # Run with RPi regular camera
  ./manage_rpi_streamer.sh rpi_ai                   # Run with RPi AI camera
  ./manage_rpi_streamer.sh synthetic                # Run with synthetic feed
  ./manage_rpi_streamer.sh --config my-config.yaml  # Run with custom config
  ./manage_rpi_streamer.sh setup-token              # Fetch token from server
  ./manage_rpi_streamer.sh setup-venv               # Create venv + install deps
  ./manage_rpi_streamer.sh enable-startup rpi       # Auto-start on boot
  ./manage_rpi_streamer.sh disable-startup          # Remove auto-start
  ./manage_rpi_streamer.sh startup-status           # Check service status

Local configs (copy from the matching *.example.yaml first):
  configs/macos-cam.yaml     - macOS camera setup
  configs/rpi-cam.yaml       - RPi regular camera (with rotation support)
  configs/rpi-ai-cam.yaml    - RPi AI camera
  configs/synthetic.yaml     - Synthetic test pattern

Tests:
  Run from rpi_streamer directory:
    python3 tests/test_ina219.py          # Test INA219 power monitor
    python3 test_fps_switching.py         # Test FPS switching
    python3 test_streamer.py              # Test core functionality
EOF
}

# Check if streamer directory exists
if [ ! -d "$STREAMER_DIR" ]; then
    echo "Error: Streamer directory not found at: $STREAMER_DIR"
    exit 1
fi

cd "$STREAMER_DIR"

# Parse arguments
if [ $# -eq 0 ] || [ "$1" = "help" ] || [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    show_help
    exit 0
fi

# ---- setup-venv: Create venv and install requirements --------------------
if [ "$1" = "setup-venv" ]; then
    echo "🐍 Setting up Python virtual environment"
    echo ""

    if [ -d "$VENV_DIR" ]; then
        echo "⚠️  Existing venv found at: $VENV_DIR"
        read -p "   Remove and recreate? [y/N]: " confirm
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            echo "   Removing old venv..."
            rm -rf "$VENV_DIR"
        else
            echo "   Keeping existing venv. Installing requirements only..."
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "→ Creating venv at: $VENV_DIR"
        python3 -m venv "$VENV_DIR" --system-site-packages
        echo "✅ venv created (with --system-site-packages)"
    fi

    echo "→ Installing requirements..."
    "$VENV_DIR/bin/pip" install --upgrade pip --quiet
    "$VENV_DIR/bin/pip" install -r "$STREAMER_DIR/requirements.txt" --quiet
    echo ""
    echo "✅ venv ready at: $VENV_DIR"
    echo "   Python: $("$VENV_DIR/bin/python3" --version)"
    echo "   Packages:"
    "$VENV_DIR/bin/pip" list 2>/dev/null | grep -iE 'opencv|numpy|websocket|pyyaml|psutil|smbus' | sed 's/^/     /'
    exit 0
fi

# ---- enable-startup: Create systemd service for auto-start on boot ------
if [ "$1" = "enable-startup" ]; then
    CAMERA_SOURCE="$2"
    if [ -z "$CAMERA_SOURCE" ]; then
        echo "❌ Camera source required."
        echo "   Usage: ./manage_rpi_streamer.sh enable-startup <source>"
        echo "   Sources: rpi, rpi_ai, synthetic, macos"
        exit 1
    fi

    # Resolve config file for the camera source
    case "$CAMERA_SOURCE" in
        macos)      STARTUP_CONFIG="configs/macos-cam.yaml" ;;
        macos_h264) STARTUP_CONFIG="configs/macos-h264.yaml" ;;
        rpi)        STARTUP_CONFIG="configs/rpi-cam.yaml" ;;
        rpi_ai)     STARTUP_CONFIG="configs/rpi-ai-cam.yaml" ;;
        synthetic)  STARTUP_CONFIG="configs/synthetic.yaml" ;;
        *)
            echo "❌ Unknown camera source: $CAMERA_SOURCE"
            exit 1
            ;;
    esac

    if [ ! -f "$STREAMER_DIR/$STARTUP_CONFIG" ]; then
        echo "❌ Config not found: $STREAMER_DIR/$STARTUP_CONFIG"
        echo "   Copy ${STARTUP_CONFIG%.yaml}.example.yaml to $STARTUP_CONFIG and edit it first."
        exit 1
    fi

    # Ensure venv exists
    if [ ! -f "$VENV_DIR/bin/python3" ]; then
        echo "❌ venv not found at: $VENV_DIR"
        echo "   Run first: ./manage_rpi_streamer.sh setup-venv"
        exit 1
    fi

    echo "🚀 Setting up auto-start on boot"
    echo "   Camera: $CAMERA_SOURCE"
    echo "   Config: $STARTUP_CONFIG"
    echo ""

    # Create systemd user service directory
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_USER_DIR"

    # Create log directory
    mkdir -p "$LOG_DIR"

    # Create the systemd service file
    SERVICE_FILE="$SYSTEMD_USER_DIR/${SERVICE_NAME}.service"
    cat > "$SERVICE_FILE" << UNIT
[Unit]
Description=RPi Camera Streamer ($CAMERA_SOURCE)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
WorkingDirectory=$STREAMER_DIR
Environment=STREAMER_CONFIG=$STARTUP_CONFIG
ExecStart=$VENV_DIR/bin/python3 $STREAMER_DIR/main.py
Restart=on-failure
RestartSec=5

# Logging — stdout/stderr go to journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=default.target
UNIT
    echo "✅ Service file created: $SERVICE_FILE"

    # Enable lingering so user services start at boot (before login)
    echo "→ Enabling loginctl linger (services start at boot)..."
    loginctl enable-linger "$(whoami)" 2>/dev/null || {
        echo "⚠️  loginctl enable-linger failed — you may need: sudo loginctl enable-linger $(whoami)"
    }

    # Reload and enable the service
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user restart "$SERVICE_NAME"

    echo ""
    echo "✅ Auto-start enabled!"
    echo ""

    # Wait briefly for the service to start, then show status
    sleep 2
    systemctl --user status "$SERVICE_NAME" --no-pager -l 2>/dev/null || true
    echo ""
    echo "📋 Commands:"
    echo "   Status:  ./manage_rpi_streamer.sh startup-status"
    echo "   Logs:    ./manage_rpi_streamer.sh startup-logs"
    echo "   Disable: ./manage_rpi_streamer.sh disable-startup"
    exit 0
fi

# ---- disable-startup: Remove auto-start service -------------------------
if [ "$1" = "disable-startup" ]; then
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SYSTEMD_USER_DIR/${SERVICE_NAME}.service"

    if [ ! -f "$SERVICE_FILE" ]; then
        echo "ℹ️  No auto-start service found (nothing to disable)"
        exit 0
    fi

    echo "🛑 Disabling auto-start"

    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload

    echo "✅ Auto-start disabled and service removed"
    echo ""
    echo "   Note: loginctl linger is still enabled."
    echo "   To disable linger: loginctl disable-linger $(whoami)"
    exit 0
fi

# ---- startup-status: Show service status and logs ------------------------
if [ "$1" = "startup-status" ]; then
    SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
    SERVICE_FILE="$SYSTEMD_USER_DIR/${SERVICE_NAME}.service"

    if [ ! -f "$SERVICE_FILE" ]; then
        echo "ℹ️  Auto-start is NOT configured"
        echo "   Enable with: ./manage_rpi_streamer.sh enable-startup <source>"
        exit 0
    fi

    echo "📊 RPi Streamer Service Status"
    echo "==============================="
    systemctl --user status "$SERVICE_NAME" --no-pager -l 2>/dev/null || true
    echo ""
    echo "📋 Recent logs (last 20 lines):"
    echo "-------------------------------"
    journalctl --user-unit "$SERVICE_NAME" --no-pager -n 20 2>/dev/null || true
    exit 0
fi

# ---- startup-logs: Show service logs -------------------------------------
if [ "$1" = "startup-logs" ]; then
    NUM_LINES="${2:-50}"
    echo "📋 RPi Streamer Logs (last $NUM_LINES lines):"
    echo "----------------------------------------------"
    journalctl --user-unit "$SERVICE_NAME" --no-pager -n "$NUM_LINES" 2>/dev/null || {
        echo "⚠️  No journal logs found. Service may not be configured."
    }
    exit 0
fi

# Handle setup-token command
if [ "$1" = "setup-token" ]; then
    echo "🔑 Camera Token Setup"
    echo "   This will fetch the camera token from the server API"
    echo ""

    read -p "Server URL (e.g. http://192.0.2.10:8000): " SERVER_URL
    SERVER_URL="${SERVER_URL%/}"

    read -p "Username: " USERNAME
    read -s -p "Password: " PASSWORD
    echo ""

    # Fetch token via DRF API using Basic Auth
    echo "→ Fetching camera token from ${SERVER_URL}/api/camera-token/ ..."
    RESPONSE=$(curl -s -w "\n%{http_code}" -u "${USERNAME}:${PASSWORD}" \
        "${SERVER_URL}/api/camera-token/" \
        -H "Accept: application/json")

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | head -n -1)

    if [ "$HTTP_CODE" != "200" ]; then
        echo "❌ Failed to fetch token (HTTP $HTTP_CODE)"
        exit 1
    fi

    # Extract token from JSON response
    TOKEN=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['camera_token'])" 2>/dev/null)
    if [ -z "$TOKEN" ]; then
        echo "❌ Failed to parse token from response"
        exit 1
    fi

    echo "✅ Token received (hidden)"

    # Ask which config to update
    echo ""
    echo "Available configs:"
    ls -1 configs/*.yaml 2>/dev/null | while read f; do echo "  $f"; done
    echo ""
    read -p "Config file to update [configs/rpi-cam.yaml]: " CONFIG_TARGET
    CONFIG_TARGET="${CONFIG_TARGET:-configs/rpi-cam.yaml}"

    if [ ! -f "$CONFIG_TARGET" ]; then
        echo "❌ Config file not found: $CONFIG_TARGET"
        echo "   Copy ${CONFIG_TARGET%.yaml}.example.yaml to $CONFIG_TARGET first."
        exit 1
    fi

    umask 077
    chmod 600 "$CONFIG_TARGET"

    # Update camera_token in YAML config
    python3 -c "
import sys
config_file = '${CONFIG_TARGET}'
token = '${TOKEN}'
lines = open(config_file).readlines()
found = False
with open(config_file, 'w') as f:
    for line in lines:
        if line.strip().startswith('camera_token:'):
            indent = len(line) - len(line.lstrip())
            f.write(' ' * indent + 'camera_token: \"' + token + '\"\n')
            found = True
        else:
            f.write(line)
    if not found:
        f.write('camera_token: \"' + token + '\"\n')
print('✅ Token saved to ' + config_file)
"
    exit 0
fi

# Handle --config option
if [ "$1" = "--config" ]; then
    if [ -z "$2" ]; then
        echo "Error: --config requires a config file path"
        exit 1
    fi
    CONFIG_FILE="$2"
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "Error: Config file not found: $CONFIG_FILE"
        exit 1
    fi
    echo "🎥 Starting RPi Camera Streamer with custom config: $CONFIG_FILE"
    export STREAMER_CONFIG="$CONFIG_FILE"
    PYTHON="python3"
    [ -f "$VENV_DIR/bin/python3" ] && PYTHON="$VENV_DIR/bin/python3"
    exec "$PYTHON" main.py
fi

# Map camera source to config file
CAMERA_SOURCE="$1"
case "$CAMERA_SOURCE" in
    macos)
        CONFIG_FILE="configs/macos-cam.yaml"
        ;;
    macos_h264)
        CONFIG_FILE="configs/macos-h264.yaml"
        ;;
    rpi)
        CONFIG_FILE="configs/rpi-cam.yaml"
        ;;
    rpi_ai)
        CONFIG_FILE="configs/rpi-ai-cam.yaml"
        ;;
    synthetic)
        CONFIG_FILE="configs/synthetic.yaml"
        ;;
    *)
        echo "Error: Unknown camera source: $CAMERA_SOURCE"
        echo ""
        show_help
        exit 1
        ;;
esac

# Check if config file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file not found: $CONFIG_FILE"
    echo "Copy ${CONFIG_FILE%.yaml}.example.yaml to $CONFIG_FILE and edit it first."
    exit 1
fi

echo "🎥 Starting RPi Camera Streamer"
echo "   Source: $CAMERA_SOURCE"
echo "   Config: $CONFIG_FILE"
echo ""

# Run the streamer (prefer venv python if available)
export STREAMER_CONFIG="$CONFIG_FILE"
PYTHON="python3"
[ -f "$VENV_DIR/bin/python3" ] && PYTHON="$VENV_DIR/bin/python3"
exec "$PYTHON" main.py
