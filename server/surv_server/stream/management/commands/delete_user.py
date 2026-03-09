"""Management command to delete a user account.

Usage:
    python manage.py delete_user                  # interactive (prompts for username)
    python manage.py delete_user alice             # asks for confirmation
    python manage.py delete_user alice --force     # skip confirmation
"""

import sys

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Delete a user account and all associated data (2FA devices, sessions)"

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", default=None)
        parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    def handle(self, *args, **options):
        User = get_user_model()

        username = options["username"]
        if not username:
            if not sys.stdin.isatty():
                raise CommandError("Username is required in non-interactive mode")
            username = input("Username to delete: ").strip()
            if not username:
                raise CommandError("Username is required")

        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' does not exist")

        is_super = user.is_superuser
        label = f"'{username}' (superuser)" if is_super else f"'{username}'"

        if not options["force"]:
            self.stderr.write(f"⚠️  This will permanently delete user {label} and all associated data.\n")
            confirm = input(f"Type the username to confirm: ").strip()
            if confirm != username:
                raise CommandError("Aborted — username did not match")

        user.delete()
        self.stdout.write(self.style.SUCCESS(f"✅ User {label} deleted\n"))
