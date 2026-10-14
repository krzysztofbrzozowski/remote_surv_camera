"""ESP8266 ingestion and private environmental dashboard endpoints."""

import csv
import hmac
import json
import math
from datetime import timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Avg, Count, Max, Min, OuterRef, Subquery
from django.db.models.functions import TruncHour, TruncMinute
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import Sensor, SensorReading

RANGES = {"1h": 1, "24h": 24, "7d": 168, "10d": 240, "30d": 720}
METRICS = ("temperature", "humidity", "pressure", "battery_voltage")


def _sensors(user):
    # Same explicit assignment policy as cameras, including staff accounts.
    return Sensor.objects.filter(allowed_users=user).distinct()


def _number(data, name, low, high, required=False):
    value = data.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number.")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}.")
    return value


@csrf_exempt  # Device endpoint: requires its own token, never accepts session auth.
@require_POST
@never_cache
def ingest(request):
    if request.content_type != "application/json":
        return JsonResponse({"error": "Use application/json."}, status=415)
    if len(request.body) > 8192:
        return JsonResponse({"error": "Measurement is too large."}, status=413)
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        mac = data.get("mac_address", "")
        if not isinstance(mac, str):
            raise ValueError("mac_address must be a string.")
        mac = mac.strip().lower().replace("-", ":")
    except (ValueError, UnicodeDecodeError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    sensor = Sensor.objects.filter(mac_address=mac, is_active=True).first()
    auth = request.headers.get("Authorization", "").split()
    token = auth[1] if len(auth) == 2 and auth[0].lower() in ("token", "bearer") else ""
    if not sensor or not sensor.token or not hmac.compare_digest(token.encode(), sensor.token.encode()):
        response = JsonResponse({"error": "Invalid device credentials."}, status=401)
        response["WWW-Authenticate"] = "Token"
        return response

    now = timezone.now()
    try:
        values = {
            "temperature": _number(data, "temperature", -80, 125, required=True),
            "humidity": _number(data, "humidity", 0, 100),
            "pressure": _number(data, "pressure", 300, 1200),
            "battery_voltage": _number(data, "adc", 0, 6),
        }
        stamp = data.get("timestamp")
        if stamp is None:
            stamp = now
        else:
            if not isinstance(stamp, str):
                raise ValueError("timestamp must be an ISO 8601 string.")
            stamp = parse_datetime(stamp)
            if stamp is None:
                raise ValueError("Invalid timestamp.")
            # The legacy ESP8266 RTC sends no offset. Time sync uses UTC.
            if timezone.is_naive(stamp):
                stamp = timezone.make_aware(stamp, datetime_timezone.utc)
            if stamp.year < 2000 or stamp > now + timedelta(minutes=5):
                raise ValueError("timestamp must be between 2000 and the current time (+5 minutes).")
    except (ValueError, TypeError, OverflowError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    with transaction.atomic():
        reading, created = SensorReading.objects.get_or_create(sensor=sensor, timestamp=stamp, defaults=values)
        # A retry must not silently replace a historical reading.
        if not created and any(getattr(reading, key) != value for key, value in values.items()):
            return JsonResponse({"error": "A different reading already exists at this timestamp."}, status=409)
        Sensor.objects.filter(pk=sensor.pk).update(last_seen=now)
    return JsonResponse({"id": reading.pk, "created": created, "timestamp": reading.timestamp.isoformat()},
                        status=201 if created else 200)


@require_GET
@never_cache
def timesync(request):
    now = timezone.now().astimezone(datetime_timezone.utc)
    return JsonResponse({"datetime_now": [now.year, now.month, now.day, now.weekday(), now.hour, now.minute, now.second, 0],
                         "timezone": "UTC"})


@require_GET
@never_cache
def time_delta(request, seconds):
    if seconds > 366 * 86400:
        return JsonResponse({"error": "Maximum delta is 366 days."}, status=400)
    stamp = timezone.now() - timedelta(seconds=seconds)
    return JsonResponse({"timestamp": stamp.isoformat()})


@login_required
@require_GET
@never_cache
def dashboard(request):
    return render(request, "stream/climate.html")


def _selection(request):
    range_name = request.GET.get("range", "24h")
    if range_name not in RANGES:
        raise ValueError("Choose range: 1h, 24h, 7d, 10d or 30d.")
    sensors = _sensors(request.user)
    sensor_id = request.GET.get("sensor")
    if sensor_id:
        sensor = sensors.filter(sensor_id=sensor_id).first()
        if sensor is None:
            raise Http404
    else:
        sensor = sensors.first()
    now = timezone.now()
    return sensors, sensor, range_name, now - timedelta(hours=RANGES[range_name]), now


def _reading(reading):
    if reading is None:
        return None
    return {"timestamp": reading.timestamp.isoformat(), **{key: getattr(reading, key) for key in METRICS}}


@login_required
@require_GET
@never_cache
def snapshot(request):
    try:
        sensors, sensor, range_name, start, now = _selection(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    latest = SensorReading.objects.filter(sensor=OuterRef("pk"), timestamp__lte=now).order_by("-timestamp")
    sensors = sensors.annotate(latest_temperature=Subquery(latest.values("temperature")[:1]),
                               latest_timestamp=Subquery(latest.values("timestamp")[:1]))
    payload = {
        "sensors": [{"id": item.sensor_id, "name": item.name, "location": item.location,
                     "online": item.is_online, "active": item.is_active,
                     "last_seen": item.last_seen.isoformat() if item.last_seen else None,
                     "temperature": item.latest_temperature,
                     "timestamp": item.latest_timestamp.isoformat() if item.latest_timestamp else None}
                    for item in sensors],
        "selected": sensor.sensor_id if sensor else None, "range": range_name,
        "start": start.isoformat(), "end": now.isoformat(), "timezone": settings.TIME_ZONE,
        "latest": None, "series": [], "recent": [], "stats": {}, "hourly": [],
    }
    if sensor is None:
        return JsonResponse(payload)
    readings = sensor.readings.filter(timestamp__gte=start, timestamp__lte=now)
    stats = readings.aggregate(count=Count("pk"), **{
        f"{key}_{op}": func(key) for key in METRICS for op, func in [("min", Min), ("max", Max), ("avg", Avg)]
    })
    # At most 1,441 minute buckets or 721 hourly buckets, regardless of sampling rate.
    trunc = TruncHour if RANGES[range_name] > 24 else TruncMinute
    series = readings.order_by().annotate(bucket=trunc("timestamp", tzinfo=datetime_timezone.utc)).values("bucket").annotate(
        **{key: Avg(key) for key in METRICS}
    ).order_by("bucket")
    hourly = sensor.readings.filter(timestamp__gte=now - timedelta(hours=24), timestamp__lte=now).order_by().annotate(
        bucket=TruncHour("timestamp", tzinfo=datetime_timezone.utc)
    ).values("bucket").annotate(temperature=Avg("temperature")).order_by("bucket")
    payload.update({
        "latest": _reading(sensor.readings.filter(timestamp__lte=now).first()),
        "series": [{"timestamp": row["bucket"].isoformat(), **{key: row[key] for key in METRICS}} for row in series],
        "recent": [_reading(row) for row in readings[:20]], "stats": stats,
        "hourly": [{"timestamp": row["bucket"].isoformat(), "temperature": row["temperature"]} for row in hourly],
        "interval": sensor.expected_interval_seconds,
    })
    return JsonResponse(payload)


class _CSVBuffer:
    def write(self, value):
        return value


@login_required
@require_GET
@never_cache
def export(request):
    try:
        _, sensor, range_name, start, now = _selection(request)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if sensor is None:
        raise Http404
    readings = sensor.readings.filter(timestamp__gte=start, timestamp__lte=now).order_by("timestamp")
    writer = csv.writer(_CSVBuffer())

    def rows():
        yield "\ufeff" + writer.writerow(["timestamp_utc", "temperature_c", "humidity_percent", "pressure_hpa", "battery_voltage_v"])
        for row in readings.iterator(chunk_size=2000):
            yield writer.writerow([row.timestamp.isoformat(), *[getattr(row, key) for key in METRICS]])

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{sensor.sensor_id}-{range_name}.csv"'
    return response
