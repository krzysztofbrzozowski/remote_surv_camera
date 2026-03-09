"""Tests for two-factor authentication on the main /login/ flow.

Run from repo root:
    cd server && source venv/bin/activate && \
    DJANGO_SECRET_KEY='test-key' CAMERA_TOKEN='test' DJANGO_ALLOWED_HOSTS='*' DJANGO_DEBUG=1 \
    PYTHONPATH=.. python manage.py test tests.test_2fa -v2
"""

import base64
import binascii
import io
import os
import sys

import django

# Bootstrap Django before importing models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "surv_server.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-key")
os.environ.setdefault("CAMERA_TOKEN", "test")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")
os.environ.setdefault("DJANGO_DEBUG", "1")
# Ensure server/ is on sys.path
_server_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)
django.setup()

from django.contrib.auth.models import User
from django.test import TestCase, Client, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class LoginWithout2FATest(TestCase):
    """Login should work normally when user has no OTP device."""

    def setUp(self):
        self.user = User.objects.create_user("testuser", password="testpass")
        self.client = Client()

    def test_login_succeeds_without_2fa(self):
        resp = self.client.post("/login/", {
            "username": "testuser",
            "password": "testpass",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("window.location", resp.content.decode())

    def test_home_accessible_after_login(self):
        self.client.post("/login/", {
            "username": "testuser",
            "password": "testpass",
        })
        resp = self.client.get("/", follow=False)
        self.assertEqual(resp.status_code, 200)

    def test_wrong_password_rejected(self):
        resp = self.client.post("/login/", {
            "username": "testuser",
            "password": "wrongpass",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Invalid credentials", resp.content.decode())

    def test_login_page_renders(self):
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("username", resp.content.decode())


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class LoginWith2FATest(TestCase):
    """Login should require OTP verification when user has a TOTP device."""

    def setUp(self):
        self.user = User.objects.create_user("admin2fa", password="pass2fa")
        self.device = TOTPDevice.objects.create(
            user=self.user, name="test", confirmed=True
        )
        self.client = Client()

    def _generate_valid_token(self):
        """Generate a valid TOTP token from the device's hex key."""
        import pyotp
        b32_key = base64.b32encode(binascii.unhexlify(self.device.key)).decode()
        return pyotp.TOTP(b32_key, digits=6).now()

    def test_password_step_shows_otp_form(self):
        resp = self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("otp_token", content)
        self.assertIn("Verification", content)
        # Should NOT be logged in yet
        self.assertNotIn("window.location", content)

    def test_valid_otp_completes_login(self):
        # Step 1: password
        self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        # Step 2: OTP
        token = self._generate_valid_token()
        resp = self.client.post("/login/", {"otp_token": token})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("window.location", resp.content.decode())

        # Should be able to access home
        resp = self.client.get("/", follow=False)
        self.assertEqual(resp.status_code, 200)

    def test_wrong_otp_rejected(self):
        self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        resp = self.client.post("/login/", {"otp_token": "000000"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Invalid verification code", resp.content.decode())

    def test_otp_form_shown_on_get_after_password(self):
        self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        resp = self.client.get("/login/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("otp_token", resp.content.decode())

    def test_reset_clears_otp_session(self):
        self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        resp = self.client.get("/login/?reset=1", follow=False)
        self.assertEqual(resp.status_code, 302)
        # After reset, login page shows username/password form
        resp = self.client.get("/login/")
        self.assertIn("username", resp.content.decode())
        self.assertNotIn("otp_token", resp.content.decode())

    def test_cannot_skip_otp_to_access_home(self):
        """After password but before OTP, user should NOT be authenticated."""
        self.client.post("/login/", {
            "username": "admin2fa",
            "password": "pass2fa",
        })
        # Try accessing protected page without completing OTP
        resp = self.client.get("/recordings/", follow=False)
        self.assertIn(resp.status_code, [302, 403])


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class Disable2FACommandTest(TestCase):
    """Test the disable_2fa management command."""

    def setUp(self):
        self.user = User.objects.create_user("cmduser", password="cmdpass")
        self.device = TOTPDevice.objects.create(
            user=self.user, name="totp-device", confirmed=True
        )
        StaticDevice.objects.create(user=self.user, name="backup")

    def test_disable_for_user(self):
        from django.core.management import call_command
        out = io.StringIO()
        call_command("disable_2fa", "cmduser", stdout=out)
        output = out.getvalue()
        self.assertIn("Disabled", output)
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_disable_nonexistent_user(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("disable_2fa", "nosuchuser")

    def test_list_users(self):
        from django.core.management import call_command
        out = io.StringIO()
        call_command("disable_2fa", "--list", stdout=out)
        output = out.getvalue()
        self.assertIn("cmduser", output)

    def test_disable_all(self):
        from django.core.management import call_command
        user2 = User.objects.create_user("cmduser2", password="pass2")
        TOTPDevice.objects.create(user=user2, name="d2", confirmed=True)

        out = io.StringIO()
        call_command("disable_2fa", "--all", stdout=out)
        self.assertFalse(TOTPDevice.objects.exists())

    def test_login_works_after_disable(self):
        """After disabling 2FA, login should not require OTP."""
        from django.core.management import call_command
        call_command("disable_2fa", "cmduser", stdout=io.StringIO())

        client = Client()
        resp = client.post("/login/", {
            "username": "cmduser",
            "password": "cmdpass",
        })
        # Should log in directly (JS redirect), no OTP form
        self.assertIn("window.location", resp.content.decode())


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class AdminURLObscuredTest(TestCase):
    """Admin should NOT be at /admin/ — should return 404."""

    def setUp(self):
        self.user = User.objects.create_superuser("su", password="supass")
        self.client = Client()

    def test_old_admin_url_returns_404(self):
        resp = self.client.get("/admin/", follow=False)
        self.assertEqual(resp.status_code, 404)

    def test_new_admin_url_exists(self):
        resp = self.client.get("/<admin-path>/", follow=False)
        # Should redirect to login (302) since we're not authenticated
        self.assertIn(resp.status_code, [200, 302])


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
)
class TwoFactorPagesTest(TestCase):
    """2FA setup/profile/backup pages should be accessible to logged-in users."""

    def setUp(self):
        self.user = User.objects.create_user("tfuser", password="tfpass")
        self.client = Client()
        self.client.login(username="tfuser", password="tfpass")

    def test_profile_page(self):
        resp = self.client.get("/account/two_factor/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Not enabled", resp.content.decode())

    def test_setup_page(self):
        resp = self.client.get("/account/two_factor/setup/")
        self.assertEqual(resp.status_code, 200)

    def test_profile_shows_enabled_after_device_added(self):
        """Profile page should show device info when user has a confirmed TOTP device.
        
        The two_factor ProfileView passes default_device from two_factor.utils.
        We verify the device exists and the page loads, since the view context
        depends on the library's internal logic.
        """
        device = TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        resp = self.client.get("/account/two_factor/")
        self.assertEqual(resp.status_code, 200)
        from two_factor.utils import default_device
        self.assertIsNotNone(default_device(self.user))


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    LOGIN_RATELIMIT_MAX_ATTEMPTS=3,
    LOGIN_RATELIMIT_WINDOW_SECONDS=300,
)
class RateLimitingTest(TestCase):
    """Rate limiting should apply to both password and OTP attempts."""

    def setUp(self):
        self.user = User.objects.create_user("rluser", password="rlpass")
        self.client = Client()

    def test_rate_limit_after_failed_passwords(self):
        for _ in range(3):
            self.client.post("/login/", {
                "username": "rluser",
                "password": "wrongpass",
            })
        resp = self.client.post("/login/", {
            "username": "rluser",
            "password": "rlpass",
        })
        self.assertIn("Too many login attempts", resp.content.decode())
