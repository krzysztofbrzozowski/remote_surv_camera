"""Environmental API contract, access isolation, history and ESP8266 compatibility."""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "surv_server.settings")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-key")
os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "*")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

import django
django.setup()

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from surv_server.stream.models import Sensor, SensorReading


@override_settings(SECURE_SSL_REDIRECT=False, CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class ClimateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("climate-user", password="test-password")
        self.sensor = Sensor.objects.create(sensor_id="garden", name="Garden", mac_address="aa:bb:cc:dd:ee:ff", token="device-secret")
        self.sensor.allowed_users.add(self.user)
        self.data = {"mac_address": self.sensor.mac_address, "temperature": 21.3, "humidity": 56.2, "pressure": 1013.2, "adc": 3.92}

    def ingest(self, data=None, token="device-secret", client=None, path="/api/measurements"):
        return (client or self.client).post(path, json.dumps(self.data if data is None else data), content_type="application/json", HTTP_AUTHORIZATION=f"Token {token}")

    def login(self):
        self.client.force_login(self.user)

    def test_firmware_payload_saves_all_measurements(self):
        response = self.ingest()
        self.assertEqual(response.status_code, 201)
        reading = SensorReading.objects.get()
        self.assertEqual(reading.temperature, 21.3)
        self.assertEqual(reading.humidity, 56.2)
        self.assertEqual(reading.pressure, 1013.2)
        self.assertEqual(reading.battery_voltage, 3.92)
        self.sensor.refresh_from_db()
        self.assertTrue(self.sensor.is_online)

    def test_temperature_only_sensor(self):
        self.assertEqual(self.ingest({"mac_address": self.sensor.mac_address, "temperature": -12.5}).status_code, 201)
        reading = SensorReading.objects.get()
        self.assertIsNone(reading.humidity)
        self.assertIsNone(reading.pressure)
        self.assertIsNone(reading.battery_voltage)

    def test_accepts_slash_url_without_redirect(self):
        self.assertEqual(self.ingest(path="/api/measurements/").status_code, 201)

    def test_mac_normalization(self):
        self.data["mac_address"] = "AA-BB-CC-DD-EE-FF"
        self.assertEqual(self.ingest().status_code, 201)

    def test_no_csrf_cookie_required_for_token_authenticated_firmware(self):
        self.assertEqual(self.ingest(client=Client(enforce_csrf_checks=True)).status_code, 201)

    def test_wrong_missing_and_cross_device_tokens_rejected(self):
        Sensor.objects.create(sensor_id="other", name="Other", mac_address="aa:bb:cc:dd:ee:00", token="other-secret")
        for token in ["", "wrong", "other-secret", "日本語"]:
            with self.subTest(token=token):
                self.assertEqual(self.ingest(token=token).status_code, 401)
        self.assertEqual(SensorReading.objects.count(), 0)

    def test_session_does_not_authorize_device_ingestion(self):
        self.login()
        self.assertEqual(self.ingest(token="").status_code, 401)

    def test_unknown_mac_does_not_register_sensor(self):
        self.data["mac_address"] = "aa:bb:cc:dd:ee:00"
        self.assertEqual(self.ingest().status_code, 401)
        self.assertEqual(Sensor.objects.count(), 1)

    def test_inactive_device_rejected(self):
        self.sensor.is_active = False
        self.sensor.save()
        self.assertEqual(self.ingest().status_code, 401)

    def test_rotating_token_invalidates_previous_token(self):
        token = self.sensor.generate_token()
        self.assertEqual(self.ingest().status_code, 401)
        self.assertEqual(self.ingest(token=token).status_code, 201)

    def test_invalid_values_do_not_store_partial_reading(self):
        for key, value in [("temperature", None), ("temperature", True), ("temperature", "21"),
                           ("temperature", float("nan")), ("temperature", float("inf")), ("temperature", -81),
                           ("temperature", 126), ("humidity", 101), ("humidity", -1), ("pressure", 1201),
                           ("pressure", 299), ("adc", 6.1), ("adc", -1), ("timestamp", "bad"),
                           ("timestamp", 123), ("timestamp", "1999-01-01T00:00:00Z"),
                           ("timestamp", "9999-12-31T23:59:59Z"), ("mac_address", [])]:
            with self.subTest(key=key, value=value):
                self.assertEqual(self.ingest({**self.data, key: value}).status_code, 400)
        self.assertFalse(SensorReading.objects.exists())

    def test_malformed_json_and_non_object(self):
        for body in ["{bad", "null", "[]", '"string"']:
            self.assertEqual(self.client.post("/api/measurements", body, content_type="application/json").status_code, 400)

    def test_media_type_size_and_method(self):
        self.assertEqual(self.client.post("/api/measurements", "bad", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post("/api/measurements", " " * 8193, content_type="application/json").status_code, 413)
        self.assertEqual(self.client.get("/api/measurements").status_code, 405)

    def test_legacy_naive_backup_timestamp_is_utc(self):
        self.data["timestamp"] = "2026-01-01T14:00:00"
        self.assertEqual(self.ingest().status_code, 201)
        self.assertEqual(SensorReading.objects.get().timestamp, datetime(2026, 1, 1, 14, tzinfo=datetime_timezone.utc))

    def test_idempotent_replay_and_conflicting_retry(self):
        self.data["timestamp"] = "2026-01-01T14:00:00Z"
        self.assertEqual(self.ingest().status_code, 201)
        response = self.ingest()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(self.ingest({**self.data, "temperature": 99}).status_code, 409)
        self.assertEqual(SensorReading.objects.count(), 1)
        self.assertEqual(SensorReading.objects.get().temperature, 21.3)

    def test_out_of_order_backup_does_not_replace_latest_measurement(self):
        self.assertEqual(self.ingest().status_code, 201)
        self.assertEqual(self.ingest({**self.data, "timestamp": "2026-01-01T14:00:00Z", "temperature": 5}).status_code, 201)
        self.login()
        self.assertEqual(self.client.get("/api/climate/").json()["latest"]["temperature"], 21.3)

    def test_firmware_time_sync_and_backup_delta_contract(self):
        with patch("surv_server.stream.climate.timezone.now", return_value=datetime(2026, 10, 13, 12, 0, tzinfo=datetime_timezone.utc)):
            response = self.client.get("/api/measurements/timesync")
            self.assertEqual(response.json()["datetime_now"], [2026, 10, 13, 1, 12, 0, 0, 0])
            delta = self.client.get("/api/measurements/timedelta/300").json()["timestamp"]
            self.assertEqual(delta, "2026-10-13T11:55:00+00:00")
            self.assertEqual(self.ingest({**self.data, "timestamp": delta}).status_code, 201)
            self.assertIn("no-store", response["Cache-Control"])

    def test_excessive_delta_rejected(self):
        self.assertEqual(self.client.get("/api/measurements/timedelta/99999999999999999").status_code, 400)

    def test_private_read_endpoints_require_login(self):
        for path in ["/climate/", "/api/climate/", "/api/climate/export/"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 302)

    def test_sensor_token_cannot_read_private_measurements(self):
        self.assertEqual(self.client.get("/api/climate/", HTTP_AUTHORIZATION="Token device-secret").status_code, 302)

    def test_assigned_user_dashboard_and_empty_readings(self):
        self.login()
        self.assertContains(self.client.get("/climate/"), "Environmental measurements and sensor status.")
        payload = self.client.get("/api/climate/").json()
        self.assertEqual(payload["selected"], "garden")
        self.assertEqual(payload["series"], [])
        self.assertIsNone(payload["latest"])
        self.assertNotIn("token", payload["sensors"][0])
        self.assertNotIn("mac_address", payload["sensors"][0])

    def test_unassigned_user_cannot_read_or_export_other_sensor(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.sensor.allowed_users.clear()
        self.login()
        payload = self.client.get("/api/climate/").json()
        self.assertEqual(payload["sensors"], [])
        self.assertIsNone(payload["selected"])
        for path in ["/api/climate/?sensor=garden", "/api/climate/export/?sensor=garden"]:
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_range_validation(self):
        self.login()
        for path in ["/api/climate/?range=forever", "/api/climate/export/?range=forever"]:
            self.assertEqual(self.client.get(path).status_code, 400)

    def test_statistics_and_aggregation_cover_all_readings(self):
        now = timezone.now()
        SensorReading.objects.bulk_create([
            SensorReading(sensor=self.sensor, timestamp=now-timedelta(minutes=10, seconds=i), temperature=value)
            for i, value in enumerate([10, 20, 30])
        ] + [SensorReading(sensor=self.sensor, timestamp=now-timedelta(days=2), temperature=99)])
        self.login()
        payload = self.client.get("/api/climate/?range=24h").json()
        self.assertEqual(payload["stats"]["count"], 3)
        self.assertEqual(payload["stats"]["temperature_avg"], 20)
        self.assertEqual(payload["stats"]["temperature_min"], 10)
        self.assertEqual(payload["stats"]["temperature_max"], 30)
        self.assertEqual(len(payload["recent"]), 3)
        payload = self.client.get("/api/climate/?range=10d").json()
        self.assertEqual(payload["stats"]["count"], 4)
        self.assertEqual(len(payload["series"]), 2)
        self.assertIsNone(payload["series"][0]["humidity"])

    def test_future_readings_not_displayed_until_their_time(self):
        SensorReading.objects.create(sensor=self.sensor, timestamp=timezone.now()+timedelta(minutes=2), temperature=35)
        self.login()
        payload = self.client.get("/api/climate/").json()
        self.assertIsNone(payload["latest"])
        self.assertIsNone(payload["sensors"][0]["temperature"])
        self.assertEqual(payload["series"], [])

    def test_csv_exports_all_rows_in_selected_range(self):
        now = timezone.now()
        SensorReading.objects.bulk_create([SensorReading(sensor=self.sensor, timestamp=now-timedelta(minutes=i), temperature=i) for i in range(25)])
        self.login()
        response = self.client.get("/api/climate/export/?sensor=garden&range=24h")
        data = b"".join(response.streaming_content).decode("utf-8-sig").splitlines()
        self.assertEqual(len(data), 26)
        self.assertIn("temperature_c", data[0])
        self.assertIn("garden-24h.csv", response["Content-Disposition"])

    def test_online_status_uses_device_interval_and_active_flag(self):
        self.sensor.last_seen = timezone.now()-timedelta(seconds=899)
        self.assertTrue(self.sensor.is_online)
        self.sensor.last_seen = timezone.now()-timedelta(seconds=901)
        self.assertFalse(self.sensor.is_online)
        self.sensor.last_seen = timezone.now()
        self.sensor.is_active = False
        self.assertFalse(self.sensor.is_online)

    def test_register_sensor_issues_token_and_assigns_reader(self):
        output = io.StringIO()
        call_command("register_sensor", mac="11-22-33-44-55-66", name="Living room", user=[self.user.username], stdout=output)
        sensor = Sensor.objects.get(mac_address="11:22:33:44:55:66")
        self.assertTrue(sensor.allowed_users.filter(pk=self.user.pk).exists())
        self.assertIn(sensor.token, output.getvalue())
        output = io.StringIO()
        call_command("register_sensor", mac=sensor.mac_address, name="Living room", stdout=output)
        self.assertNotIn(sensor.token, output.getvalue())
        self.assertIn("unchanged", output.getvalue())

    def test_invalid_registration_rolls_back(self):
        for args in [{"mac": "not-a-mac"}, {"mac": "11:22:33:44:55:66", "interval": 0},
                     {"mac": "11:22:33:44:55:66", "user": ["missing"]}]:
            with self.subTest(args=args), self.assertRaises(CommandError):
                call_command("register_sensor", name="New", stdout=io.StringIO(), **args)
        self.assertEqual(Sensor.objects.count(), 1)
