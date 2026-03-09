#!/usr/bin/env bash
# RPi Streamer Management Script
# Usage: ./manage_rpi_streamer.sh [camera_source] [options]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAMER_DIR="$SCRIPT_DIR/rpi_streamer"

show_help() {
    cat << EOF
RPi Camera Streamer Management Script

Usage: ./manage_rpi_streamer.sh [camera_source] [options]

Camera Sources:
  macos         macOS/USB camera with motion detection
  rpi           Raspberry Pi regular camera (Camera Module 3) with motion detection
  rpi_ai        Raspberry Pi AI Camera (IMX500) with hardware detection
  synthetic     Synthetic test pattern (no camera required)

Options:
  --config FILE Use custom config file (overrides camera source)
  setup-token   Fetch camera token from server API and save to config
  
Examples:
  ./manage_rpi_streamer.sh macos                    # Run with macOS camera
  ./manage_rpi_streamer.sh rpi                      # Run with RPi regular camera
  ./manage_rpi_streamer.sh rpi_ai                   # Run with RPi AI camera
  ./manage_rpi_streamer.sh synthetic                # Run with synthetic feed
  ./manage_rpi_streamer.sh --config my-config.yaml  # Run with custom config
  ./manage_rpi_streamer.sh setup-token              # Fetch token from server

Available Configs:
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

# Handle setup-token command
if [ "$1" = "setup-token" ]; then
    echo "🔑 Camera Token Setup"
    echo "   This will fetch the camera token from the server API"
    echo ""

    read -p "Server URL (e.g. http://192.168.1.100:8000): " SERVER_URL
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
        echo "   Response: $BODY"
        exit 1
    fi

    # Extract token from JSON response
    TOKEN=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['camera_token'])" 2>/dev/null)
    if [ -z "$TOKEN" ]; then
        echo "❌ Failed to parse token from response"
        echo "   Response: $BODY"
        exit 1
    fi

    echo "✅ Token received: ${TOKEN:0:8}..."

    # Ask which config to update
    echo ""
    echo "Available configs:"
    ls -1 configs/*.yaml 2>/dev/null | while read f; do echo "  $f"; done
    echo ""
    read -p "Config file to update [configs/rpi-cam.yaml]: " CONFIG_TARGET
    CONFIG_TARGET="${CONFIG_TARGET:-configs/rpi-cam.yaml}"

    if [ ! -f "$CONFIG_TARGET" ]; then
        echo "❌ Config file not found: $CONFIG_TARGET"
        exit 1
    fi

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
    exec python3 main.py
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
    exit 1
fi

echo "🎥 Starting RPi Camera Streamer"
echo "   Source: $CAMERA_SOURCE"
echo "   Config: $CONFIG_FILE"
echo ""

# Run the streamer
export STREAMER_CONFIG="$CONFIG_FILE"
exec python3 main.py
