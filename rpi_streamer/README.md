# RPi Camera Streamer

Streams camera feed + metadata to Django server via WebSocket with dynamic FPS based on AI detection.

## Features

- **H.264 Hardware Encoding**: RPi camera uses V4L2 M2M H.264 encoder (massive bandwidth savings vs JPEG)
- **Dynamic FPS**: 1 FPS (idle) → 15-30 FPS (detection/motion) → 1 FPS (idle)
- **RPi Camera Module 3**: Standard Raspberry Pi camera with motion detection
- **RPi AI Camera**: Hardware detection via IMX500 sensor
- **Camera Rotation**: Support for 90°, 180°, 270° rotation
- **Motion Detection**: Software-based motion detection for regular cameras
- **INA219 Power Monitoring**: Real-time voltage, current, power, and battery percentage
- **2K Resolution**: 1920x1080
- **Simulated Detection**: macOS camera for testing

## Quick Start

### Synthetic (no camera)
```bash
STREAMER_CONFIG=configs/synthetic.yaml python3 main.py
```

### macOS Camera (with simulated detection)
```bash
./start-macos-streamer.sh
# or
STREAMER_CONFIG=configs/macos-cam.yaml python3 main.py
```

### RPi Regular Camera (Camera Module 3)
```bash
STREAMER_CONFIG=configs/rpi-cam.yaml python3 main.py
```

### RPi AI Camera (IMX500)
```bash
# Use camera_source: "rpi_ai" in config
STREAMER_CONFIG=configs/rpi-ai-cam.yaml python3 main.py
```

## Structure

```
rpi_streamer/
├── main.py              # Main streamer
├── sources/             # Camera implementations
│   ├── base.py          # Abstract base
│   ├── synthetic.py     # Test pattern
│   ├── macos.py         # USB camera + sim
│   ├── rpi_regular.py   # Regular RPi camera
│   └── rpi_ai.py        # RPi AI Camera
├── configs/             # Configuration files
└── tests/               # Unit tests
```

## Configuration

```yaml
camera_source: "rpi"      # "rpi" for regular, "rpi_ai" for AI camera
frame_width: 1920         # 2K resolution
frame_height: 1080
fps: 30                   # Max FPS (when detecting)
idle_fps: 1               # FPS when idle
h264_bitrate: 2000000     # H.264 bitrate (rpi source only)
jpeg_quality: 70
camera_rotation: 0        # Optional: 0, 90, 180, or 270 degrees
```

### Camera Rotation

If your camera is physically mounted at an angle, use the `camera_rotation` parameter:

```yaml
camera_rotation: 90   # Rotate 90° clockwise
# Options: 0, 90, 180, 270
```

This ensures the video appears correctly oriented regardless of physical mounting.

## Testing

```bash
python3 test_fps_switching.py  # FPS switching tests
python3 test_streamer.py        # Core functionality tests
```

## Detection

- **RPi Regular** (`rpi`): H.264 HW encoding + software motion detection (1 FPS → 15 FPS)
- **RPi AI Camera** (`rpi_ai`): Hardware detection via IMX500 neural network (1 FPS → 30 FPS, JPEG)
- **macOS**: JPEG + motion detection with 30s simulation cycle
- **Synthetic**: JPEG, no detection (fixed 1 FPS)

### Motion Detection Tuning

For `rpi` and `macos` sources, you can adjust motion detection sensitivity:

```yaml
motion_threshold: 25      # Pixel difference threshold (lower = more sensitive)
min_motion_area: 500      # Minimum pixels that must change (lower = more sensitive)
detection_cooldown: 3.0   # Seconds to stay at high FPS after motion stops
```

When detection/motion active: 15-30 FPS. When idle: 1 FPS.

### H.264 vs JPEG Bandwidth Comparison

| Mode | Encoding | Idle (1 FPS) | Active (15 FPS) |
|------|----------|-------------|-----------------|
| RPi  | H.264    | ~250 Kbps   | ~2 Mbps         |
| Others | JPEG   | ~1.5 Mbps   | ~24 Mbps        |

## Power Monitoring (INA219)

The streamer automatically detects and uses INA219 power monitor if available (I2C address 0x43 by default).

**Monitored metrics:**
- Load Voltage (V)
- Current (A)
- Power (W)
- Battery Percentage (estimated from voltage)

The power data is included in the metadata sent to the server and can be displayed on the web interface.

**Note:** On macOS or systems without INA219, power monitoring is disabled and metrics show as unavailable.

