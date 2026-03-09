"""Management command to create a user with 2FA (TOTP) pre-configured.

Usage:
    python manage.py create_user                        # interactive
    python manage.py create_user admin --superuser      # superuser, prompted for password
    python manage.py create_user bob --password secret  # non-interactive
"""

import io
import sys
import tty
import termios

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django_otp.plugins.otp_totp.models import TOTPDevice


def _prompt_password(prompt: str) -> str:
    """Read password from TTY, echoing '*' per character."""
    sys.stderr.write(prompt)
    sys.stderr.flush()

    # Fall back to plain getpass when not on a real TTY
    if not sys.stdin.isatty():
        import getpass
        return getpass.getpass("")

    password = []
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stderr.write("\n")
                break
            elif ch in ("\x7f", "\x08"):  # backspace / del
                if password:
                    password.pop()
                    sys.stderr.write("\b \b")
            elif ch == "\x03":  # Ctrl-C
                sys.stderr.write("\n")
                raise KeyboardInterrupt
            else:
                password.append(ch)
                sys.stderr.write("*")
            sys.stderr.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return "".join(password)


class Command(BaseCommand):
    help = "Create a user with 2FA (TOTP) enabled by default"

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", default=None)
        parser.add_argument("--password", default=None)
        parser.add_argument("--email", default="")
        parser.add_argument("--superuser", action="store_true")
        parser.add_argument("--no-qr", action="store_true", help="Skip QR code output")

    def handle(self, *args, **options):
        User = get_user_model()

        username = options["username"]
        if not username:
            username = input("Username: ").strip()
            if not username:
                raise CommandError("Username is required")

        if User.objects.filter(username=username).exists():
            raise CommandError(f"User '{username}' already exists")

        password = options["password"]
        if not password:
            password = _prompt_password("Password: ")
            confirm = _prompt_password("Confirm password: ")
            if password != confirm:
                raise CommandError("Passwords do not match")

        email = options["email"]
        if not email and sys.stdin.isatty():
            email = input("Email (optional): ").strip()

        is_super = options["superuser"]
        if is_super:
            user = User.objects.create_superuser(username, email, password)
        else:
            user = User.objects.create_user(username, email, password)

        # Create a confirmed TOTP device
        device = TOTPDevice.objects.create(
            user=user,
            name=f"{username}-totp",
            confirmed=True,
        )

        # Build provisioning URI (compatible with Google Authenticator, Apple Passwords)
        uri = device.config_url

        self.stdout.write(self.style.SUCCESS(
            f"\n✅ User '{username}' created {'(superuser) ' if is_super else ''}with 2FA enabled\n"
        ))

        if not options["no_qr"]:
            self._print_qr(uri)

        self.stdout.write(f"📋 Provisioning URI (manual entry):\n   {uri}\n")
        self.stdout.write(
            "📱 Scan with Google Authenticator / Apple Passwords / Authy\n"
            "   or enter the key manually in your TOTP app.\n"
        )

    def _print_qr(self, uri: str):
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(uri)
            qr.make(fit=True)
            f = io.StringIO()
            qr.print_ascii(out=f, invert=True)
            self.stdout.write(f.getvalue())
        except Exception:
            pass  # qrcode optional — URI still printed below
