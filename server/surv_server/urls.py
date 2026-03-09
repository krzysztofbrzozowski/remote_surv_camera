from django.contrib import admin
from django.contrib.admin import AdminSite
from django.urls import include, path
from surv_server.stream import views

from django_otp import user_has_device
from two_factor.urls import urlpatterns as tf_urls


class AdminSiteOTP(AdminSite):
    """Admin site that requires OTP only when the user has a 2FA device."""

    def has_permission(self, request):
        if not super().has_permission(request):
            return False
        # If user has no OTP devices, allow through without verification
        if not user_has_device(request.user, confirmed=True):
            return True
        return request.user.is_verified()

    def login(self, request, extra_context=None):
        from django.conf import settings
        from django.contrib.auth import REDIRECT_FIELD_NAME
        from django.contrib.auth.views import redirect_to_login
        from django.shortcuts import resolve_url
        from django.utils.http import url_has_allowed_host_and_scheme

        redirect_to = request.POST.get(
            REDIRECT_FIELD_NAME, request.GET.get(REDIRECT_FIELD_NAME)
        )
        if not redirect_to or not url_has_allowed_host_and_scheme(
            url=redirect_to, allowed_hosts=[request.get_host()]
        ):
            redirect_to = resolve_url(settings.LOGIN_REDIRECT_URL)
        return redirect_to_login(redirect_to)


admin.site.__class__ = AdminSiteOTP

urlpatterns = [
    # two_factor URLs (login, setup, QR, etc.) — needed for admin 2FA
    path("", include(tf_urls)),
    path("<admin-path>/", admin.site.urls),
    path("", views.live_view, name="live"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("recordings/", views.recordings_list, name="recordings"),
    path("recordings/delete-all/", views.recording_delete_all, name="recording_delete_all"),
    path("recordings/<str:filename>/", views.recording_playback, name="recording_playback"),
    path("recordings/<str:filename>/download/", views.recording_download, name="recording_download"),
    path("recordings/<str:filename>/frame/<int:frame_number>/", views.recording_frame, name="recording_frame"),
    path("recordings/<str:filename>/file/", views.recording_download_file, name="recording_download_file"),
    path("recordings/<str:filename>/mp4/", views.recording_h264_mp4, name="recording_h264_mp4"),
    path("recordings/<str:filename>/delete/", views.recording_delete, name="recording_delete"),
    path("api/cameras/", views.api_cameras, name="api_cameras"),
    path("admin-stats/", views.admin_stats_view, name="admin_stats"),
    path("api/server-stats/", views.api_server_stats, name="api_server_stats"),
    path("api/stats-history/", views.api_stats_history, name="api_stats_history"),
    path("api/network-usage/", views.api_network_usage, name="api_network_usage"),
    path("api/camera-token/", views.api_camera_token, name="api_camera_token"),
    path("admin-logs/", views.admin_logs_view, name="admin_logs"),
    path("api/server-logs/", views.api_server_logs, name="api_server_logs"),
    path("admin-daily-logs/", views.admin_daily_logs_view, name="admin_daily_logs"),
    path("admin-daily-logs/<str:date>/<str:filename>/", views.admin_daily_log_file, name="admin_daily_log_file"),
]
