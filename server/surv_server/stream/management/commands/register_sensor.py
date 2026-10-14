from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from surv_server.stream.models import Sensor


class Command(BaseCommand):
    help = "Register an environmental sensor and issue its device token."

    def add_arguments(self, parser):
        parser.add_argument("--mac", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--sensor-id")
        parser.add_argument("--location", default="")
        parser.add_argument("--user", action="append", default=[], help="Username allowed to read this sensor (repeatable)")
        parser.add_argument("--interval", type=int, default=300)
        parser.add_argument("--rotate-token", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        mac = options["mac"].strip().lower().replace("-", ":")
        users = []
        for username in options["user"]:
            try:
                users.append(get_user_model().objects.get(username=username))
            except get_user_model().DoesNotExist:
                raise CommandError(f"Unknown user: {username}")
        sensor = Sensor.objects.filter(mac_address=mac).first()
        created = sensor is None
        if created:
            sensor = Sensor(mac_address=mac, sensor_id=options["sensor_id"] or "sensor-" + mac.replace(":", ""),
                            name=options["name"], location=options["location"], expected_interval_seconds=options["interval"])
            try:
                sensor.full_clean()
            except ValidationError as exc:
                raise CommandError(str(exc))
            sensor.save()
        sensor.allowed_users.add(*users)
        if created or options["rotate_token"] or not sensor.token:
            token = sensor.generate_token()
            self.stdout.write(self.style.SUCCESS(f"Sensor: {sensor.sensor_id}"))
            self.stdout.write(f"AUTH_TOKEN = '{token}'")
        else:
            self.stdout.write(f"Sensor {sensor.sensor_id} already registered; token unchanged. Use --rotate-token to replace it.")
        self.stdout.write("API_url = 'https://YOUR-SERVER/api/measurements' (no trailing slash)")
