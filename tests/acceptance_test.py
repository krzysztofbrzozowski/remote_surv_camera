#!/usr/bin/env python3
"""Acceptance test: quick connectivity and streaming smoke tests.

Works against both local-dev and production. Does NOT require SSH — use
test_data_integrity.py for full sent-vs-received verification with SSH.

Usage:
    # Against local-dev (ws://)
    python3 tests/acceptance_test.py --local

    # Against production (wss://)
    TEST_CAMERA_TOKEN=<token> python3 tests/acceptance_test.py

    # Specific test
    python3 tests/acceptance_test.py --local connect
    python3 tests/acceptance_test.py --local 100mb
    python3 tests/acceptance_test.py --local reliability

Environment:
    TEST_SERVER_WS     WebSocket URL (auto-set by --local)
    TEST_CAMERA_TOKEN  Camera auth token (or reads from rpi_streamer/configs/.token)
"""
import asyncio
import json
import os
import ssl
import sys
import time

import websockets

# ── Configuration ──────────────────────────────────────────────────────────

LOCAL_WS = "ws://localhost:8080/ws/camera/feed/"
PROD_WS = ""  # Set TEST_SERVER_WS explicitly for remote tests
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "rpi_streamer", "configs", ".token")
LOCAL_TOKEN = "local-development-placeholder-token"


def load_token(is_local: bool) -> str:
    if is_local:
        return LOCAL_TOKEN
    env_token = os.environ.get("TEST_CAMERA_TOKEN")
    if env_token:
        return env_token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    raise RuntimeError(
        "No camera token. Set TEST_CAMERA_TOKEN or create rpi_streamer/configs/.token"
    )


async def send_data(ws_url: str, token: str, target_mb: float,
                    encoding: str = "h264", bitrate_mbps: float = 50,
                    camera_id: str = "acceptance_test",
                    ssl_ctx=None) -> tuple[int, int, float]:
    """Send target_mb of binary data. Returns (bytes_sent, frames, elapsed_s)."""
    uri = f"{ws_url}?camera_id={camera_id}"
    target_bytes = int(target_mb * 1024 * 1024)
    chunk_size = 65536
    frame_data = os.urandom(chunk_size)
    total_sent = 0
    frames = 0
    start = time.time()

    async with websockets.connect(
        uri, ssl=ssl_ctx,
        additional_headers={"Authorization": f"Token {token}"},
        max_size=2**22, ping_interval=20, ping_timeout=60,
        close_timeout=10,
    ) as ws:
        # Initialize recording with correct metadata
        await ws.send(json.dumps({"encoding": encoding, "camera_id": camera_id}))

        while total_sent < target_bytes:
            remaining = target_bytes - total_sent
            if remaining < chunk_size:
                frame_data = os.urandom(remaining)
            await ws.send(frame_data)
            total_sent += len(frame_data)
            frames += 1

            # Rate limit
            elapsed = time.time() - start
            expected = total_sent / (bitrate_mbps * 1024 * 1024 / 8)
            if elapsed < expected:
                await asyncio.sleep(expected - elapsed)

            if frames % max(1, int(10 * 1024 * 1024 / chunk_size)) == 0:
                elapsed = time.time() - start
                rate = total_sent / elapsed / 1024 / 1024
                pct = total_sent / target_bytes * 100
                print(f"  [{pct:5.1f}%] {total_sent/1024/1024:.1f} MB "
                      f"in {elapsed:.1f}s ({rate:.1f} MB/s)")

        await asyncio.sleep(2)

    elapsed = time.time() - start
    return total_sent, frames, elapsed


# ── Tests ──────────────────────────────────────────────────────────────────

async def test_connect(ws_url: str, token: str, ssl_ctx):
    """Verify WebSocket connects, server accepts auth, and recording starts."""
    print("\n  TEST: connect")
    camera_id = "acceptance_connect"
    uri = f"{ws_url}?camera_id={camera_id}"
    try:
        async with websockets.connect(
            uri, ssl=ssl_ctx,
            additional_headers={"Authorization": f"Token {token}"},
            max_size=2**22, ping_interval=20, close_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"encoding": "h264", "camera_id": camera_id}))
            await ws.send(os.urandom(1024))
            await asyncio.sleep(1)
        print("  PASS: connected, sent metadata + 1 frame, disconnected cleanly")
        return True
    except Exception as e:
        print(f"  FAIL: {e}")
        return False


async def test_100mb(ws_url: str, token: str, ssl_ctx):
    """Send 100 MB of H.264 data."""
    print("\n  TEST: 100mb")
    sent, frames, elapsed = await send_data(
        ws_url, token, 100, bitrate_mbps=80, camera_id="acceptance_100mb",
        ssl_ctx=ssl_ctx,
    )
    rate = sent / elapsed / 1024 / 1024
    print(f"  PASS: sent {sent:,} bytes ({frames} frames) in {elapsed:.1f}s ({rate:.1f} MB/s)")
    return True


async def test_1gb(ws_url: str, token: str, ssl_ctx):
    """Send 1 GB of H.264 data."""
    print("\n  TEST: 1gb")
    sent, frames, elapsed = await send_data(
        ws_url, token, 1024, bitrate_mbps=150, camera_id="acceptance_1gb",
        ssl_ctx=ssl_ctx,
    )
    rate = sent / elapsed / 1024 / 1024
    print(f"  PASS: sent {sent:,} bytes ({frames} frames) in {elapsed:.1f}s ({rate:.1f} MB/s)")
    return True


async def test_reliability(ws_url: str, token: str, ssl_ctx):
    """Sustained streaming with periodic voluntary disconnects (2 min)."""
    print("\n  TEST: reliability (2 min sustained, reconnect every 20s)")
    camera_id = "acceptance_reliability"
    frame_data = os.urandom(32768)
    total_sent = 0
    reconnects = 0
    errors = 0
    start = time.time()
    duration = 120

    while time.time() - start < duration:
        try:
            uri = f"{ws_url}?camera_id={camera_id}"
            async with websockets.connect(
                uri, ssl=ssl_ctx,
                additional_headers={"Authorization": f"Token {token}"},
                max_size=2**22, ping_interval=20, close_timeout=10,
            ) as ws:
                await ws.send(json.dumps({
                    "encoding": "h264", "camera_id": camera_id,
                }))
                session_start = time.time()
                while (time.time() - session_start < 20
                       and time.time() - start < duration):
                    await ws.send(frame_data)
                    total_sent += len(frame_data)
                    await asyncio.sleep(0.02)
                reconnects += 1
                print(f"    session #{reconnects}: "
                      f"total={total_sent/1024/1024:.1f} MB, "
                      f"elapsed={time.time()-start:.0f}s")
        except Exception as e:
            errors += 1
            print(f"    error #{errors}: {e}")
            await asyncio.sleep(2)

    elapsed = time.time() - start
    passed = errors == 0
    status = "PASS" if passed else "FAIL"
    print(f"  {status}: {total_sent/1024/1024:.1f} MB in {elapsed:.0f}s, "
          f"{reconnects} reconnects, {errors} errors")
    return passed


# ── Main ───────────────────────────────────────────────────────────────────

TESTS = {
    "connect": test_connect,
    "100mb": test_100mb,
    "1gb": test_1gb,
    "reliability": test_reliability,
}


async def main():
    args = sys.argv[1:]
    is_local = "--local" in args
    args = [a for a in args if a != "--local"]
    test_name = args[0] if args else "all"

    ws_url = os.environ.get("TEST_SERVER_WS", LOCAL_WS if is_local else PROD_WS)
    if not ws_url:
        sys.exit("Set TEST_SERVER_WS for remote tests, or use --local")
    token = load_token(is_local)
    ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss") else None

    print(f"{'='*60}")
    print(f"  ACCEPTANCE TESTS")
    print(f"  Server: {ws_url}")
    print(f"  Mode:   {'local-dev' if is_local else 'production'}")
    print(f"{'='*60}")

    results = {}
    if test_name == "all":
        for name, fn in TESTS.items():
            results[name] = await fn(ws_url, token, ssl_ctx)
    elif test_name in TESTS:
        results[test_name] = await TESTS[test_name](ws_url, token, ssl_ctx)
    else:
        print(f"\nUnknown test: {test_name}")
        print(f"Available: {', '.join(TESTS.keys())}, all")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    all_passed = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        all_passed = all_passed and passed
        print(f"  {'✅' if passed else '❌'} {name}")

    print(f"\n  {'All tests passed' if all_passed else 'SOME TESTS FAILED'}")
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
