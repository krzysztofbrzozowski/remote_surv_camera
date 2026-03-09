from django.contrib import admin
from .models import Camera, NetworkHourlyStat, Recording


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
