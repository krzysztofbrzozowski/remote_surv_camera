import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ValueError(
        "DJANGO_SECRET_KEY environment variable is required. "
        "Generate one with: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
    )

DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() in ("true", "1")

_allowed = os.environ.get("DJANGO_ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()] if _allowed else []

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    "rest_framework",
    "surv_server.stream",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "surv_server.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "surv_server" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ASGI_APPLICATION = "surv_server.asgi.application"

# Channel layers
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
            "capacity": 500,  # Increased for 30 FPS streaming
            "expiry": 10,
            "group_expiry": 86400,  # 24h — membership refreshed periodically by consumers
        },
    },
}

_DB_DIR = BASE_DIR / "data"
_DB_DIR.mkdir(exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _DB_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TZ", "Europe/Warsaw")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "surv_server" / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"

# Two-Factor Authentication (TOTP — Google Authenticator / Apple Passwords)
TWO_FACTOR_PATCH_ADMIN = True
TWO_FACTOR_LOGIN_TIMEOUT = 600  # seconds to complete 2FA login flow

# Camera settings
# Camera settings — per-device tokens in Django Admin are preferred;
# CAMERA_TOKEN env var is a legacy fallback (optional).
CAMERA_TOKEN = os.environ.get("CAMERA_TOKEN", "")

RECORDINGS_DIR = Path(os.environ.get("RECORDINGS_DIR", BASE_DIR / "recordings"))
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
RECORDING_CHUNK_SIZE_MB = int(os.environ.get("RECORDING_CHUNK_SIZE_MB", "100"))

# Security headers
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_BROWSER_XSS_FILTER = True

if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h and h != "localhost"]

# Session security
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = 2592000  # 30 days
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# Login rate limiting (max attempts per IP per window)
LOGIN_RATELIMIT_MAX_ATTEMPTS = int(os.environ.get("LOGIN_RATELIMIT_MAX_ATTEMPTS", "5"))
LOGIN_RATELIMIT_WINDOW_SECONDS = int(os.environ.get("LOGIN_RATELIMIT_WINDOW_SECONDS", "300"))

# WebRTC ICE — no external STUN by default (LAN & VPS with public IP need none).
# Set STUN_SERVER env var only when behind NAT, e.g. "stun:stun.l.google.com:19302"
STUN_SERVER = os.environ.get("STUN_SERVER", "")

# TURN relay — required for viewers behind Symmetric NAT / CGNAT (e.g. Orange PL 5G).
# When direct UDP fails, TURN relays media through a known server.
TURN_SERVER = os.environ.get("TURN_SERVER", "")
TURN_USERNAME = os.environ.get("TURN_USERNAME", "")
TURN_PASSWORD = os.environ.get("TURN_PASSWORD", "")

# Valid camera ID pattern (alphanumeric, dashes, underscores only)
CAMERA_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")

# Valid recording filename pattern
RECORDING_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+\.(mjpeg|h264)$")

# Network interface to monitor (auto-detected if empty).
# Set to specific interface name (e.g. "eth0", "enp1s0") to avoid counting
# Docker bridge/veth/loopback traffic which inflates the aggregate counters.
NETWORK_INTERFACE = os.environ.get("NETWORK_INTERFACE", "")

# Path to SSH auth log for login attempt tracking on the admin dashboard.
# In Docker production the host's auth.log is mounted at /host-logs/auth.log.
# Leave empty to auto-detect (/host-logs/auth.log, /var/log/auth.log, journalctl).
SSH_AUTH_LOG_PATH = os.environ.get("SSH_AUTH_LOG_PATH", "")

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}
