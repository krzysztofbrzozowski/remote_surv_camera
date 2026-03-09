"""Daily logging for the RPi streamer.

Creates daily log directories under rpi_streamer/logs/YYYY-MM-DD/ with:
  - data_usage.log : RX/TX bytes logged every hour
  - streamer.log   : application log output for the day

Sensitive values (tokens, passwords, secrets) are automatically masked.
"""

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

LOGS_ROOT = Path(__file__).resolve().parent / "logs"

# Patterns to mask in log messages
_SENSITIVE_PATTERNS = [
    re.compile(r'((?:Authorization|Bearer)\s*[:=]?\s*(?:Token\s+|Bearer\s+)?)([^\s"]{4})[^\s"]*', re.IGNORECASE),
    re.compile(r'("?(?:token|password|secret|secret_key|camera_token|api_key)"?\s*[:=]\s*)"?([^",\s}{]{4})[^",\s}{]*"?', re.IGNORECASE),
]


def sanitize(text: str) -> str:
    """Replace sensitive values with masked version, keeping first 4 chars."""
    for pat in _SENSITIVE_PATTERNS:
        text = pat.sub(lambda m: f'{m.group(1)}"{m.group(2)}***"', text)
    return text


class _SanitizingFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        return sanitize(msg)


class DailyLogger:
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None
        self._snapshot_rx = 0
        self._snapshot_tx = 0
        self._app_logger = None
        self._current_day = None
        self._file_handler = None

    def _day_dir(self, dt: datetime = None) -> Path:
        dt = dt or datetime.now()
        d = LOGS_ROOT / dt.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- Data usage (RX/TX) ------------------------------------------------
    @staticmethod
    def _get_net_bytes():
        c = psutil.net_io_counters()
        return c.bytes_recv, c.bytes_sent

    def _log_data_usage(self):
        now = datetime.now()
        rx, tx = self._get_net_bytes()
        delta_rx = rx - self._snapshot_rx
        delta_tx = tx - self._snapshot_tx
        self._snapshot_rx = rx
        self._snapshot_tx = tx

        log_path = self._day_dir(now) / "data_usage.log"
        line = (
            f"{now.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"RX: {delta_rx / (1024*1024):.2f} MB  "
            f"TX: {delta_tx / (1024*1024):.2f} MB  "
            f"(total RX: {rx / (1024*1024):.1f} MB  "
            f"TX: {tx / (1024*1024):.1f} MB)\n"
        )
        with open(log_path, "a") as f:
            f.write(line)

    def _background_loop(self):
        self._snapshot_rx, self._snapshot_tx = self._get_net_bytes()
        self._log_data_usage()
        while not self._stop_event.wait(timeout=3600):
            self._log_data_usage()

    # -- Application logger ------------------------------------------------
    def _ensure_file_handler(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_day == today and self._file_handler:
            return
        if self._file_handler and self._app_logger:
            self._app_logger.removeHandler(self._file_handler)
            self._file_handler.close()

        log_file = self._day_dir() / "streamer.log"
        self._file_handler = logging.FileHandler(str(log_file))
        self._file_handler.setLevel(logging.DEBUG)
        self._file_handler.setFormatter(
            _SanitizingFormatter("%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
        )
        if self._app_logger:
            self._app_logger.addHandler(self._file_handler)
        self._current_day = today

    def get_app_logger(self) -> logging.Logger:
        if self._app_logger is None:
            self._app_logger = logging.getLogger("streamer")
            self._ensure_file_handler()
        return self._app_logger

    def _rotate_check_loop(self):
        while not self._stop_event.wait(timeout=60):
            self._ensure_file_handler()

    # -- Start / Stop ------------------------------------------------------
    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._background_loop, daemon=True, name="streamer-data-usage"
        )
        self._thread.start()
        self._rotate_thread = threading.Thread(
            target=self._rotate_check_loop, daemon=True, name="streamer-log-rotate"
        )
        self._rotate_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._file_handler:
            self._file_handler.close()
