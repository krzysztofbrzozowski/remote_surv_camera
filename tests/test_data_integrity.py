#!/usr/bin/env python3
"""Data integrity test: verifies bytes sent == bytes received/recorded by server.

Sends known amounts of data via WebSocket, then verifies via SSH that the
server recorded the correct amount on disk. Max acceptable difference: 2%.

Usage:
    python3 test_data_integrity.py                 # Run all tests
    python3 test_data_integrity.py quick            # Quick 50MB smoke test
    python3 test_data_integrity.py 100mb
    python3 test_data_integrity.py 500mb
    python3 test_data_integrity.py 1gb
    python3 test_data_integrity.py 5gb
    python3 test_data_integrity.py reconnect
    python3 test_data_integrity.py small_frames
    python3 test_data_integrity.py large_frames

Environment:
    TEST_SERVER_WS     WebSocket URL (required)
    TEST_SERVER_SSH    SSH destination (required)
    TEST_SSH_KEY       SSH key path
    TEST_CAMERA_TOKEN  Camera auth token (or reads from rpi_streamer/configs/.token)
"""
import asyncio
import json
import os
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass, field

import websockets

# ── Configuration ──────────────────────────────────────────────────────────
SERVER_WS = os.environ.get("TEST_SERVER_WS", "")
SSH_DEST = os.environ.get("TEST_SERVER_SSH", "")
SSH_KEY = os.path.expanduser(os.environ.get("TEST_SSH_KEY", "~/.ssh/id_ed25519"))
DOCKER_CONTAINER = os.environ.get("TEST_DOCKER_CONTAINER", "server-server-1")
RECORDINGS_DIR = "/app/recordings"

TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "rpi_streamer", "configs", ".token")
MAX_TOLERANCE = 0.02  # 2% max difference

ssl_ctx = ssl.create_default_context()


def load_token():
    env_token = os.environ.get("TEST_CAMERA_TOKEN")
    if env_token:
        return env_token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    raise RuntimeError("No camera token found. Set TEST_CAMERA_TOKEN or create rpi_streamer/configs/.token")


if not SERVER_WS or not SSH_DEST:
    raise RuntimeError("Set TEST_SERVER_WS and TEST_SERVER_SSH explicitly before running remote tests")

TOKEN = load_token()
AUTH_HEADERS = {"Authorization": f"Token {TOKEN}"}


# ── SSH helper ─────────────────────────────────────────────────────────────

def ssh_cmd(cmd: str, timeout: int = 30) -> str:
    """Run a command on the server via SSH."""
    full_cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        SSH_DEST, cmd
    ]
    result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"SSH command failed: {result.stderr.strip()}")
    return result.stdout.strip()


def docker_cmd(cmd: str, timeout: int = 30) -> str:
    """Run a command inside the server Docker container via SSH."""
    return ssh_cmd(f"docker exec {DOCKER_CONTAINER} {cmd}", timeout=timeout)


def get_recording_bytes(camera_id: str) -> tuple[int, int]:
    """Get total recording bytes (disk + DB) for a camera via SSH.

    Returns (disk_bytes, db_bytes).
    """
    # Disk: sum of recording file sizes
    disk_output = docker_cmd(
        f"find {RECORDINGS_DIR}/{camera_id} -type f "
        f"\\( -name '*.h264' -o -name '*.mjpeg' \\) "
        f"-exec stat -c '%s' {{}} + 2>/dev/null | paste -sd+ | bc"
    )
    disk_bytes = int(disk_output) if disk_output else 0

    # DB: sum of size_bytes from Recording model
    db_output = docker_cmd(
        f"python manage.py shell -c \""
        f"from surv_server.stream.models import Recording; "
        f"recs = Recording.objects.filter(camera__camera_id='{camera_id}'); "
        f"print(sum(r.size_bytes for r in recs))\""
    )
    db_bytes = int(db_output) if db_output else 0

    return disk_bytes, db_bytes


def get_recording_count(camera_id: str) -> int:
    """Get number of recordings for a camera via SSH."""
    output = docker_cmd(
        f"python manage.py shell -c \""
        f"from surv_server.stream.models import Recording; "
        f"print(Recording.objects.filter(camera__camera_id='{camera_id}').count())\""
    )
    return int(output) if output else 0


def get_network_stats_snapshot() -> tuple[int, int]:
    """Get current physical interface RX/TX bytes from the server (enp1s0 only)."""
    output = ssh_cmd(
        "cat /proc/net/dev | awk '/enp1s0/{split($0,a,\":\"); split(a[2],b); print b[1],b[9]}'"
    )
    parts = output.split()
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return 0, 0


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class SendResult:
    total_bytes_sent: int = 0
    total_frames: int = 0
    elapsed_seconds: float = 0
    rate_mbps: float = 0
    errors: list = field(default_factory=list)


@dataclass
class VerifyResult:
    test_name: str = ""
    bytes_sent: int = 0
    frames_sent: int = 0
    # Expected recording bytes (accounting for encoding overhead)
    expected_bytes: int = 0
    # Actual recorded
    bytes_disk: int = 0
    bytes_db: int = 0
    # Network level
    net_rx_delta: int = 0
    # Computed
    diff_disk_pct: float = 0
    diff_db_pct: float = 0
    diff_net_pct: float = 0
    passed: bool = False
    details: str = ""


# ── Core send function ─────────────────────────────────────────────────────

async def send_data(target_mb: float, encoding: str = "h264",
                    bitrate_mbps: float = 50, chunk_size: int = 65536,
                    camera_id: str = "test_integ") -> SendResult:
    """Send exactly target_mb of binary data via WebSocket."""
    uri = f"{SERVER_WS}?camera_id={camera_id}"
    target_bytes = int(target_mb * 1024 * 1024)
    result = SendResult()

    frame_data = os.urandom(chunk_size)
    start_time = time.time()

    try:
        async with websockets.connect(
            uri, ssl=ssl_ctx, additional_headers=AUTH_HEADERS,
            max_size=2**22, ping_interval=20, ping_timeout=60,
            close_timeout=10
        ) as ws:
            # Send metadata with encoding to initialize recording
            await ws.send(json.dumps({
                "encoding": encoding,
                "camera_id": camera_id,
            }))

            while result.total_bytes_sent < target_bytes:
                remaining = target_bytes - result.total_bytes_sent
                if remaining < chunk_size:
                    frame_data = os.urandom(remaining)

                await ws.send(frame_data)
                result.total_bytes_sent += len(frame_data)
                result.total_frames += 1

                # Rate limiting
                elapsed = time.time() - start_time
                expected_time = result.total_bytes_sent / (bitrate_mbps * 1024 * 1024 / 8)
                if elapsed < expected_time:
                    await asyncio.sleep(expected_time - elapsed)

                # Progress every ~50MB
                if result.total_frames % max(1, int(50 * 1024 * 1024 / chunk_size)) == 0:
                    elapsed = time.time() - start_time
                    rate = result.total_bytes_sent / elapsed / 1024 / 1024
                    pct = result.total_bytes_sent / target_bytes * 100
                    print(f"  [{pct:5.1f}%] Sent {result.total_bytes_sent/1024/1024:.1f} MB "
                          f"in {elapsed:.1f}s ({rate:.1f} MB/s)")

            # Allow server to flush recording before disconnect
            await asyncio.sleep(2)

    except Exception as e:
        result.errors.append(str(e))

    result.elapsed_seconds = time.time() - start_time
    if result.elapsed_seconds > 0:
        result.rate_mbps = result.total_bytes_sent / result.elapsed_seconds / 1024 / 1024

    return result


# ── Verification ──────────────────────────────────────────────────────────

def verify_transfer(test_name: str, send_result: SendResult,
                    baseline_disk: int, baseline_db: int,
                    baseline_net_rx: int, current_net_rx: int,
                    encoding: str = "h264",
                    camera_id: str = "test_integ") -> VerifyResult:
    """Compare sent bytes vs recorded bytes. Max tolerance: 2%."""
    vr = VerifyResult(test_name=test_name)
    vr.bytes_sent = send_result.total_bytes_sent
    vr.frames_sent = send_result.total_frames

    # Wait for server to finalize recordings
    print(f"  Waiting for server to finalize recordings...")
    time.sleep(5)

    # Get current recording sizes
    disk_now, db_now = get_recording_bytes(camera_id)
    vr.bytes_disk = disk_now - baseline_disk
    vr.bytes_db = db_now - baseline_db

    # Network delta (physical interface only)
    vr.net_rx_delta = current_net_rx - baseline_net_rx

    # For h264: recording = exactly what was sent (raw passthrough)
    # For jpeg: recording = sent + (4 bytes header per frame)
    vr.expected_bytes = vr.bytes_sent
    if encoding == "jpeg":
        vr.expected_bytes = vr.bytes_sent + (4 * vr.frames_sent)

    # Calculate differences
    if vr.expected_bytes > 0:
        vr.diff_disk_pct = abs(vr.bytes_disk - vr.expected_bytes) / vr.expected_bytes
        vr.diff_db_pct = abs(vr.bytes_db - vr.expected_bytes) / vr.expected_bytes
    else:
        vr.diff_disk_pct = 1.0 if vr.bytes_disk != 0 else 0.0
        vr.diff_db_pct = 1.0 if vr.bytes_db != 0 else 0.0

    # Network-level: expect ~5-10% overhead from TCP/TLS/WebSocket framing
    if vr.bytes_sent > 0:
        vr.diff_net_pct = abs(vr.net_rx_delta - vr.bytes_sent) / vr.bytes_sent

    # Pass criteria: disk and DB within 2%
    vr.passed = (vr.diff_disk_pct <= MAX_TOLERANCE and
                 vr.diff_db_pct <= MAX_TOLERANCE)

    vr.details = (
        f"Sent: {vr.bytes_sent:,} bytes ({vr.bytes_sent/1024/1024:.2f} MB), "
        f"{vr.frames_sent} frames\n"
        f"  Expected recording: {vr.expected_bytes:,} bytes (encoding={encoding})\n"
        f"  Recorded (disk): {vr.bytes_disk:,} bytes "
        f"(diff: {vr.diff_disk_pct*100:.4f}%) "
        f"{'✓' if vr.diff_disk_pct <= MAX_TOLERANCE else '✗'}\n"
        f"  Recorded (DB):   {vr.bytes_db:,} bytes "
        f"(diff: {vr.diff_db_pct*100:.4f}%) "
        f"{'✓' if vr.diff_db_pct <= MAX_TOLERANCE else '✗'}\n"
        f"  Exact DB match:  {vr.bytes_db} == {vr.expected_bytes}? "
        f"{'✓ EXACT' if vr.bytes_db == vr.expected_bytes else '✗ MISMATCH'}\n"
        f"  Exact disk match:{vr.bytes_disk} == {vr.expected_bytes}? "
        f"{'✓ EXACT' if vr.bytes_disk == vr.expected_bytes else '✗ MISMATCH'}\n"
        f"  Network RX delta: {vr.net_rx_delta:,} bytes "
        f"(overhead: {vr.diff_net_pct*100:.1f}% — TCP/TLS/WS framing, informational only)"
    )

    return vr


# ── Print helpers ──────────────────────────────────────────────────────────

def print_header(name: str, target_mb: float):
    print(f"\n{'='*70}")
    print(f"  TEST: {name} — Send {target_mb:.0f} MB, verify ≤{MAX_TOLERANCE*100:.0f}% diff")
    print(f"{'='*70}")


def print_result(vr: VerifyResult):
    status = "✅ PASS" if vr.passed else "❌ FAIL"
    print(f"\n  {status}: {vr.test_name}")
    print(f"  {vr.details}")
    if not vr.passed:
        print(f"  ⚠ TOLERANCE EXCEEDED: max {MAX_TOLERANCE*100:.0f}%, "
              f"got disk={vr.diff_disk_pct*100:.4f}% db={vr.diff_db_pct*100:.4f}%")


def print_send_result(sr: SendResult):
    print(f"  Sent: {sr.total_bytes_sent:,} bytes ({sr.total_bytes_sent/1024/1024:.2f} MB)")
    print(f"  Frames: {sr.total_frames}")
    print(f"  Time: {sr.elapsed_seconds:.1f}s ({sr.rate_mbps:.1f} MB/s)")
    if sr.errors:
        print(f"  ⚠ Errors: {sr.errors}")


# ── Test runner ────────────────────────────────────────────────────────────

async def run_test(name: str, target_mb: float, encoding: str = "h264",
                   bitrate_mbps: float = 50, chunk_size: int = 65536) -> VerifyResult:
    """Generic test runner: baseline → send → verify."""
    camera_id = f"test_integ_{name}"
    print_header(name, target_mb)

    # Baseline
    baseline_disk, baseline_db = get_recording_bytes(camera_id)
    baseline_net_rx, _ = get_network_stats_snapshot()
    print(f"  Baseline: disk={baseline_disk:,} db={baseline_db:,}")

    # Send
    sr = await send_data(target_mb, encoding=encoding, bitrate_mbps=bitrate_mbps,
                         chunk_size=chunk_size, camera_id=camera_id)
    print_send_result(sr)

    # Capture network after send
    current_net_rx, _ = get_network_stats_snapshot()

    # Verify
    vr = verify_transfer(name, sr, baseline_disk, baseline_db,
                         baseline_net_rx, current_net_rx,
                         encoding=encoding, camera_id=camera_id)
    print_result(vr)
    return vr


# ── Test cases ────────────────────────────────────────────────────────────

async def test_quick():
    return await run_test("Quick_50MB_h264", 50, encoding="h264", bitrate_mbps=100)


async def test_100mb():
    return await run_test("100MB_h264", 100, encoding="h264", bitrate_mbps=100)


async def test_100mb_jpeg():
    return await run_test("100MB_jpeg", 100, encoding="jpeg", bitrate_mbps=100)


async def test_500mb():
    return await run_test("500MB_h264", 500, encoding="h264", bitrate_mbps=200)


async def test_1gb():
    return await run_test("1GB_h264", 1024, encoding="h264", bitrate_mbps=200)


async def test_5gb():
    return await run_test("5GB_h264", 5120, encoding="h264", bitrate_mbps=300,
                          chunk_size=131072)


async def test_small_frames():
    """Stress per-frame overhead with 512-byte frames."""
    return await run_test("Small_512B_frames", 50, encoding="h264",
                          bitrate_mbps=50, chunk_size=512)


async def test_large_frames():
    """Test with 1MB frames."""
    return await run_test("Large_1MB_frames", 200, encoding="h264",
                          bitrate_mbps=200, chunk_size=1024*1024)


async def test_reconnect():
    """Send data across 5 reconnections, verify total recorded."""
    camera_id = "test_integ_reconnect"
    print_header("Reconnect 5×60MB", 300)

    baseline_disk, baseline_db = get_recording_bytes(camera_id)
    baseline_net_rx, _ = get_network_stats_snapshot()
    print(f"  Baseline: disk={baseline_disk:,} db={baseline_db:,}")

    total_bytes_sent = 0
    total_frames = 0

    for i in range(5):
        print(f"\n  Session {i+1}/5: sending 60 MB...")
        sr = await send_data(60, encoding="h264", bitrate_mbps=150,
                             camera_id=camera_id)
        total_bytes_sent += sr.total_bytes_sent
        total_frames += sr.total_frames
        print(f"    Sent: {sr.total_bytes_sent:,} bytes, {sr.total_frames} frames")
        if sr.errors:
            print(f"    ⚠ Errors: {sr.errors}")
        await asyncio.sleep(2)

    current_net_rx, _ = get_network_stats_snapshot()

    combined = SendResult(total_bytes_sent=total_bytes_sent, total_frames=total_frames)
    vr = verify_transfer("Reconnect_5x60MB", combined,
                         baseline_disk, baseline_db,
                         baseline_net_rx, current_net_rx,
                         encoding="h264", camera_id=camera_id)
    print_result(vr)
    return vr


# ── Main ──────────────────────────────────────────────────────────────────

TESTS = {
    "quick": test_quick,
    "100mb": test_100mb,
    "100mb_jpeg": test_100mb_jpeg,
    "500mb": test_500mb,
    "1gb": test_1gb,
    "5gb": test_5gb,
    "reconnect": test_reconnect,
    "small_frames": test_small_frames,
    "large_frames": test_large_frames,
}


async def main():
    test_name = sys.argv[1] if len(sys.argv) > 1 else "all"

    print(f"╔{'═'*68}╗")
    print(f"║  DATA INTEGRITY TEST — sent vs received (≤{MAX_TOLERANCE*100:.0f}% tolerance)       ║")
    print(f"║  Server: {SERVER_WS:<57}║")
    print(f"║  SSH:    {SSH_DEST:<57}║")
    print(f"╚{'═'*68}╝")

    # Preflight: verify SSH and WebSocket connectivity
    print("\nPreflight checks...")
    try:
        ssh_cmd("echo ok")
        print(f"  ✓ SSH connection to {SSH_DEST}")
    except Exception as e:
        print(f"  ✗ SSH failed: {e}")
        sys.exit(1)

    try:
        docker_cmd("python manage.py shell -c 'print(1)'")
        print(f"  ✓ Docker container {DOCKER_CONTAINER} accessible")
    except Exception as e:
        print(f"  ✗ Docker exec failed: {e}")
        sys.exit(1)

    net_rx, net_tx = get_network_stats_snapshot()
    print(f"  ✓ Network snapshot: RX={net_rx/1024**3:.2f} GB  TX={net_tx/1024**3:.2f} GB")

    results = {}
    all_start = time.time()

    if test_name == "all":
        for name in ["quick", "100mb", "100mb_jpeg", "500mb",
                      "small_frames", "large_frames", "reconnect", "1gb"]:
            vr = await TESTS[name]()
            results[name] = vr
    elif test_name in TESTS:
        vr = await TESTS[test_name]()
        results[test_name] = vr
    else:
        print(f"\nUnknown test: {test_name}")
        print(f"Available: {', '.join(TESTS.keys())}, all")
        sys.exit(1)

    total_time = time.time() - all_start

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  SUMMARY  (total time: {total_time:.0f}s)")
    print(f"{'='*70}")

    all_passed = True
    for name, vr in results.items():
        status = "✅ PASS" if vr.passed else "❌ FAIL"
        all_passed = all_passed and vr.passed
        print(f"  {status}  {vr.test_name:<25}  "
              f"sent={vr.bytes_sent/1024/1024:>8.1f} MB  "
              f"disk={vr.bytes_disk/1024/1024:>8.1f} MB  "
              f"diff={vr.diff_disk_pct*100:>6.3f}%")

    print(f"\n  {'🎉 ALL TESTS PASSED' if all_passed else '💥 SOME TESTS FAILED'}")
    print(f"  Tolerance: ≤{MAX_TOLERANCE*100:.0f}%")

    # Save results
    results_json = {
        name: {
            "test_name": vr.test_name,
            "bytes_sent": vr.bytes_sent,
            "bytes_disk": vr.bytes_disk,
            "bytes_db": vr.bytes_db,
            "expected_bytes": vr.expected_bytes,
            "frames_sent": vr.frames_sent,
            "diff_disk_pct": round(vr.diff_disk_pct * 100, 4),
            "diff_db_pct": round(vr.diff_db_pct * 100, 4),
            "net_rx_delta": vr.net_rx_delta,
            "passed": vr.passed,
        }
        for name, vr in results.items()
    }

    out_path = "/tmp/data_integrity_results.json"
    with open(out_path, "w") as f:
        json.dump(results_json, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
