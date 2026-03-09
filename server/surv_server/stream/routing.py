from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/camera/feed/$", consumers.CameraConsumer.as_asgi()),
    re_path(r"ws/viewer/$", consumers.ViewerConsumer.as_asgi()),
]
