from django.contrib import admin
from .models import Camera, NetworkHourlyStat, Recording, Sensor, SensorReading


def generate_tokens(modeladmin, request, queryset):
    for camera in queryset:
        camera.generate_token()
generate_tokens.short_description = "Generate new auth token"


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ["camera_id", "name", "is_online", "last_seen", "display_rotation", "max_storage_gb", "storage_used_gb", "token_preview"]
    list_editable = ["name", "display_rotation", "max_storage_gb"]
    readonly_fields = ["token", "is_online", "last_seen", "storage_used_gb"]
    actions = [generate_tokens]
    filter_horizontal = ["allowed_users"]

    def token_preview(self, obj):
        return obj.token[:12] + "…" if obj.token else "—"
    token_preview.short_description = "Token"


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ["filename", "camera", "started_at", "ended_at", "size_mb", "frame_count"]
    list_filter = ["camera"]
    readonly_fields = ["filename", "camera", "started_at", "ended_at", "size_bytes", "frame_count"]


@admin.register(NetworkHourlyStat)
class NetworkHourlyStatAdmin(admin.ModelAdmin):
    list_display = ["date", "hour", "rx_mb", "tx_mb", "cpu_avg", "mem_avg", "sample_count"]
    list_filter = ["date"]
    readonly_fields = ["date", "hour", "rx_bytes", "tx_bytes", "cpu_sum", "mem_sum", "sample_count"]


@admin.register(Sensor)
class SensorAdmin(admin.ModelAdmin):
    list_display = ["name", "sensor_id", "location", "mac_address", "is_active", "is_online", "last_seen"]
    list_filter = ["is_active"]
    search_fields = ["name", "sensor_id", "mac_address", "location"]
    readonly_fields = ["token", "last_seen", "created_at"]
    filter_horizontal = ["allowed_users"]
    actions = [generate_tokens]
    prepopulated_fields = {"sensor_id": ("name",)}

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not obj.token:
            obj.generate_token()


@admin.register(SensorReading)
class SensorReadingAdmin(admin.ModelAdmin):
    list_display = ["sensor", "timestamp", "temperature", "humidity", "pressure", "battery_voltage"]
    list_filter = ["sensor"]
    date_hierarchy = "timestamp"
    readonly_fields = ["sensor", "timestamp", "received_at", "temperature", "humidity", "pressure", "battery_voltage"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
