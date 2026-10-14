import secrets
from datetime import timedelta

from django.conf import settings
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


class NetworkHourlyStat(models.Model):
    """Persistent hourly server stats (network, CPU, memory) accumulated from psutil."""
    date = models.DateField()
    hour = models.IntegerField()  # 0-23
    rx_bytes = models.BigIntegerField(default=0)
    tx_bytes = models.BigIntegerField(default=0)
    cpu_sum = models.FloatField(default=0)
    mem_sum = models.FloatField(default=0)
    sample_count = models.IntegerField(default=0)

    class Meta:
        unique_together = ("date", "hour")
        ordering = ["date", "hour"]
        indexes = [models.Index(fields=["date"])]

    def __str__(self):
        return f"{self.date} {self.hour:02d}:00 — RX {self.rx_mb:.1f} MB / TX {self.tx_mb:.1f} MB"

    @property
    def rx_mb(self):
        return self.rx_bytes / (1024 * 1024)

    @property
    def tx_mb(self):
        return self.tx_bytes / (1024 * 1024)

    @property
    def cpu_avg(self):
        return self.cpu_sum / self.sample_count if self.sample_count else 0

    @property
    def mem_avg(self):
        return self.mem_sum / self.sample_count if self.sample_count else 0


class Camera(models.Model):
    camera_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200, blank=True)
    token = models.CharField(max_length=64, blank=True, help_text="Auth token for this camera. Generate via admin action.")
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(null=True, blank=True)
    display_rotation = models.IntegerField(default=0, help_text="Display rotation in degrees (0, 90, 180, 270)")
    max_storage_gb = models.FloatField(default=10.0, help_text="Max recording storage in GB. Oldest recordings pruned when exceeded.")
    allowed_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="allowed_cameras",
        help_text="Users allowed to view this camera. Only listed users can access it.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["camera_id"]

    def __str__(self):
        return self.name or self.camera_id

    def generate_token(self):
        self.token = secrets.token_urlsafe(32)
        self.save(update_fields=["token"])
        return self.token

    @property
    def storage_used_bytes(self):
        return self.recordings.aggregate(total=models.Sum("size_bytes"))["total"] or 0

    @property
    def storage_used_gb(self):
        return round(self.storage_used_bytes / (1024 ** 3), 2)


class Recording(models.Model):
    camera = models.ForeignKey(Camera, on_delete=models.CASCADE, related_name="recordings")
    filename = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    frame_count = models.IntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return self.filename

    @property
    def size_mb(self):
        return round(self.size_bytes / (1024 * 1024), 1)


class Sensor(models.Model):
    """Provisioned environmental station; tokens never grant browser access."""

    sensor_id = models.SlugField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200, blank=True)
    mac_address = models.CharField(max_length=17, unique=True, validators=[
        RegexValidator(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$", "Use a MAC address such as aa:bb:cc:dd:ee:ff.")
    ])
    token = models.CharField(max_length=64, blank=True, editable=False)
    is_active = models.BooleanField(default=True)
    expected_interval_seconds = models.PositiveIntegerField(default=300, validators=[MinValueValidator(10)])
    last_seen = models.DateTimeField(null=True, blank=True)
    allowed_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="allowed_sensors")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "sensor_id"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.mac_address = self.mac_address.strip().lower().replace("-", ":")
        super().save(*args, **kwargs)

    def generate_token(self):
        self.token = secrets.token_urlsafe(32)
        self.save(update_fields=["token"])
        return self.token

    @property
    def is_online(self):
        return bool(self.is_active and self.last_seen and self.last_seen >= timezone.now() - timedelta(
            seconds=self.expected_interval_seconds * 3
        ))


class SensorReading(models.Model):
    sensor = models.ForeignKey(Sensor, on_delete=models.CASCADE, related_name="readings")
    timestamp = models.DateTimeField(default=timezone.now)
    received_at = models.DateTimeField(auto_now_add=True)
    temperature = models.FloatField()
    humidity = models.FloatField(null=True, blank=True)
    pressure = models.FloatField(null=True, blank=True, help_text="Pressure in hPa")
    battery_voltage = models.FloatField(null=True, blank=True, help_text="Battery voltage (firmware adc field)")

    class Meta:
        ordering = ["-timestamp", "-pk"]
        constraints = [models.UniqueConstraint(fields=["sensor", "timestamp"], name="unique_sensor_timestamp")]

    def __str__(self):
        return f"{self.sensor}: {self.temperature} °C @ {self.timestamp.isoformat()}"
