"""Management command to disable 2FA for a user via SSH/CLI.

Usage:
    python manage.py disable_2fa admin          # disable for user 'admin'
    python manage.py disable_2fa --all          # disable for all users
    python manage.py disable_2fa --list         # list users with 2FA enabled
"""

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django_otp import devices_for_user


class Command(BaseCommand):
    help = "Disable two-factor authentication for a user"

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", default=None, help="Username to disable 2FA for")
        parser.add_argument("--all", action="store_true", help="Disable 2FA for all users")
        parser.add_argument("--list", action="store_true", help="List users with 2FA enabled")

    def handle(self, *args, **options):
        User = get_user_model()

        if options["list"]:
            self._list_users(User)
            return

        if options["all"]:
            count = 0
            for user in User.objects.all():
                removed = self._disable_for_user(user)
                if removed:
                    count += 1
            self.stdout.write(self.style.SUCCESS(f"Disabled 2FA for {count} user(s)"))
            return

        username = options["username"]
        if not username:
            raise CommandError("Provide a username, or use --all / --list")

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found")

        removed = self._disable_for_user(user)
        if removed:
            self.stdout.write(self.style.SUCCESS(f"✅ Disabled 2FA for '{username}' ({removed} device(s) removed)"))
        else:
            self.stdout.write(f"ℹ️  User '{username}' has no 2FA devices")

    def _disable_for_user(self, user):
        devices = list(devices_for_user(user))
        for device in devices:
            device.delete()
        return len(devices)

    def _list_users(self, User):
        self.stdout.write("Users with 2FA enabled:")
        found = False
        for user in User.objects.all():
            devices = list(devices_for_user(user))
            if devices:
                found = True
                device_names = ", ".join(type(d).__name__ for d in devices)
                self.stdout.write(f"  {user.username} — {len(devices)} device(s): {device_names}")
        if not found:
            self.stdout.write("  (none)")
