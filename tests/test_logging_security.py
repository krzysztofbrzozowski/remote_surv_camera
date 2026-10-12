"""Tests for logging security: sanitization, daily log creation, and web access.

Run from repo root:
    cd server && source venv/bin/activate && \
    DJANGO_SECRET_KEY='test-key' CAMERA_TOKEN='test' DJANGO_ALLOWED_HOSTS='*' DJANGO_DEBUG=1 \
    PYTHONPATH=.. python manage.py test tests.test_logging_security -v2
"""

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import mock

import django

# Bootstrap Django before importing models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "surv_server.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-key")
os.environ.setdefault("CAMERA_TOKEN", "test")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")
os.environ.setdefault("DJANGO_DEBUG", "1")
_server_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)
django.setup()

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings


class SanitizationTest(TestCase):
    """Test that sensitive data is properly masked in log output."""

    def _sanitize(self, text):
        import importlib
        import sys
        # Import from the server's daily_logger
        server_dir = Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        if "daily_logger" in sys.modules:
            mod = importlib.reload(sys.modules["daily_logger"])
        else:
            mod = importlib.import_module("daily_logger")
        return mod.sanitize(text)

    def test_camera_token_masked(self):
        text = 'camera_token: test-only-camera-token-00000000000000000000'
        result = self._sanitize(text)
        self.assertNotIn("test-only-camera-token-00000000000000000000", result)
        self.assertIn("test***", result)

    def test_password_masked(self):
        text = 'password: SuperSecret123!'
        result = self._sanitize(text)
        self.assertNotIn("SuperSecret123!", result)
        self.assertIn("Supe***", result)

    def test_secret_key_masked(self):
        text = 'secret_key = "django-insecure-abc123xyz456"'
        result = self._sanitize(text)
        self.assertNotIn("django-insecure-abc123xyz456", result)

    def test_api_key_masked(self):
        text = 'api_key: "test-only-api-key-0000000000000000"'
        result = self._sanitize(text)
        self.assertNotIn("test-only-api-key-0000000000000000", result)
        self.assertIn("test***", result)

    def test_authorization_header_masked(self):
        text = 'Authorization: Token rpi-abc123def456ghi789jkl'
        result = self._sanitize(text)
        self.assertNotIn("rpi-abc123def456ghi789jkl", result)
        self.assertIn("rpi-***", result)

    def test_bearer_token_masked(self):
        text = 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
        result = self._sanitize(text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", result)

    def test_json_token_masked(self):
        text = '"token": "test-only-camera-token-00000000000000000000"'
        result = self._sanitize(text)
        self.assertNotIn("test-only-camera-token-00000000000000000000", result)
        self.assertIn("test***", result)

    def test_non_sensitive_text_unchanged(self):
        text = "2026-02-24 12:00:00 INFO Camera connected successfully"
        result = self._sanitize(text)
        self.assertEqual(text, result)

    def test_multiple_secrets_masked(self):
        text = 'camera_token: abc123xyz password: secret999 api_key: mykey456'
        result = self._sanitize(text)
        self.assertNotIn("abc123xyz", result)
        self.assertNotIn("secret999", result)
        self.assertNotIn("mykey456", result)

    def test_short_values_not_over_masked(self):
        """Normal words like 'token' in regular text shouldn't be masked."""
        text = "Checking token validity for camera connection"
        result = self._sanitize(text)
        self.assertEqual(text, result)


class DailyLogCreationTest(TestCase):
    """Test that daily logger creates the expected directory structure and files."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_data_usage_log_created(self):
        import importlib
        import sys
        server_dir = Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        if "daily_logger" in sys.modules:
            mod = importlib.reload(sys.modules["daily_logger"])
        else:
            mod = importlib.import_module("daily_logger")

        # Patch LOGS_ROOT to use temp dir
        with mock.patch.object(mod, "LOGS_ROOT", Path(self.tmp_dir)):
            logger = mod.DailyLogger()
            logger._log_data_usage()

            today = datetime.now().strftime("%Y-%m-%d")
            log_file = Path(self.tmp_dir) / today / "data_usage.log"
            self.assertTrue(log_file.exists(), f"Expected {log_file} to exist")

            content = log_file.read_text()
            self.assertIn("RX:", content)
            self.assertIn("TX:", content)
            self.assertIn("MB", content)

    def test_day_directory_format(self):
        import importlib
        import sys
        server_dir = Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        if "daily_logger" in sys.modules:
            mod = importlib.reload(sys.modules["daily_logger"])
        else:
            mod = importlib.import_module("daily_logger")

        with mock.patch.object(mod, "LOGS_ROOT", Path(self.tmp_dir)):
            logger = mod.DailyLogger()
            day_dir = logger._day_dir()
            self.assertTrue(day_dir.is_dir())
            # Directory name should be YYYY-MM-DD format
            self.assertRegex(day_dir.name, r"^\d{4}-\d{2}-\d{2}$")

    def test_sanitizing_formatter(self):
        import importlib
        import logging
        import sys
        server_dir = Path(__file__).resolve().parent.parent / "server"
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
        if "daily_logger" in sys.modules:
            mod = importlib.reload(sys.modules["daily_logger"])
        else:
            mod = importlib.import_module("daily_logger")

        formatter = mod._SanitizingFormatter("%(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg='camera_token: secretvalue1234567890',
            args=(), exc_info=None,
        )
        output = formatter.format(record)
        self.assertNotIn("secretvalue1234567890", output)
        self.assertIn("secr***", output)


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class DailyLogsWebAccessTest(TestCase):
    """Test web-based daily log file browser requires auth and sanitizes content."""

    def setUp(self):
        self.user = User.objects.create_user("loguser", password="logpass")
        self.client = Client()
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_daily_logs_requires_login(self):
        resp = self.client.get("/admin-daily-logs/", follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)

    def test_daily_logs_accessible_authenticated(self):
        self.client.login(username="loguser", password="logpass")
        resp = self.client.get("/admin-daily-logs/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Daily Logs", resp.content.decode())

    def test_log_file_requires_login(self):
        resp = self.client.get("/admin-daily-logs/2026-02-24/server.log/", follow=False)
        self.assertEqual(resp.status_code, 302)

    def test_log_file_content_sanitized(self):
        log_dir = Path(self.tmp_dir) / "2026-02-24"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "server.log"
        log_file.write_text(
            'camera_token: test-only-camera-token-00000000000000000000\n'
            'password: mysuperpassword123\n'
            'Normal log line\n'
        )

        self.client.login(username="loguser", password="logpass")

        # Patch the module-level _LOGS_DIR in the views module
        from surv_server.stream import views
        original = views._LOGS_DIR
        views._LOGS_DIR = Path(self.tmp_dir)
        try:
            resp = self.client.get("/admin-daily-logs/2026-02-24/server.log/")
            self.assertEqual(resp.status_code, 200)
            content = resp.content.decode()
            # Sensitive values must be masked
            self.assertNotIn("test-only-camera-token-00000000000000000000", content)
            self.assertNotIn("mysuperpassword123", content)
            self.assertIn("test***", content)
            self.assertIn("mysu***", content)
            # Normal text unchanged
            self.assertIn("Normal log line", content)
        finally:
            views._LOGS_DIR = original

    def test_path_traversal_blocked_date(self):
        self.client.login(username="loguser", password="logpass")
        resp = self.client.get("/admin-daily-logs/../../etc/server.log")
        self.assertIn(resp.status_code, [404, 400, 302])

    def test_path_traversal_blocked_filename(self):
        self.client.login(username="loguser", password="logpass")
        resp = self.client.get("/admin-daily-logs/2026-02-24/../../etc/passwd")
        self.assertIn(resp.status_code, [404, 400, 302])

    def test_daily_logs_no_emoji(self):
        """Daily logs page should not have calendar emoji."""
        from surv_server.stream import views
        original = views._LOGS_DIR
        log_dir = Path(self.tmp_dir) / "2026-02-24"
        log_dir.mkdir(parents=True)
        (log_dir / "test.log").write_text("test")
        views._LOGS_DIR = Path(self.tmp_dir)
        try:
            self.client.login(username="loguser", password="logpass")
            resp = self.client.get("/admin-daily-logs/")
            content = resp.content.decode()
            self.assertNotIn("\U0001f4c5", content)  # 📅
            self.assertIn("2026-02-24", content)
        finally:
            views._LOGS_DIR = original
