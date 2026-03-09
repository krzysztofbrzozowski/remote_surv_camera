"""Token auth middleware for WebSocket connections from RPi camera."""
import hmac
import logging
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.sessions.models import Session
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)
from urllib.parse import parse_qs


class TokenAuthMiddleware:
    """Authenticates camera WebSocket connections via Token header or query param.
    Also supports session_key query param for viewer authentication (Safari WS fix).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode()

        query = parse_qs(scope.get("query_string", b"").decode())
        query_token = query.get("token", [None])[0]

        token = None
        if auth.startswith("Token "):
            token = auth.split(" ", 1)[1]
        elif query_token:
            token = query_token

        # Authenticate: check per-device tokens first, then global fallback
        scope["camera_authenticated"] = False
        if token is not None:
            camera_id = query.get("camera_id", [None])[0]
            if camera_id:
                scope["camera_authenticated"] = await self._check_camera_token(camera_id, token)
            # Fallback to global CAMERA_TOKEN for backward compatibility
            if not scope["camera_authenticated"]:
                global_token = getattr(settings, "CAMERA_TOKEN", "")
                if global_token and hmac.compare_digest(token, global_token):
                    scope["camera_authenticated"] = True
        scope["is_viewer"] = not scope["camera_authenticated"]

        # Fallback: authenticate viewer via session_key query param
        # when cookie-based auth failed (Safari WebSocket cookie issue)
        session_key = query.get("session_key", [None])[0]
        if session_key and scope["is_viewer"]:
            is_auth = getattr(scope.get("user"), "is_authenticated", False)
            if not is_auth:
                user = await self._get_user_from_session(session_key)
                if user:
                    scope["user"] = user

        return await self.app(scope, receive, send)

    @database_sync_to_async
    def _get_user_from_session(self, session_key):
        try:
            session = Session.objects.get(session_key=session_key)
            uid = session.get_decoded().get("_auth_user_id")
            if uid:
                return User.objects.get(pk=uid)
        except (Session.DoesNotExist, User.DoesNotExist):
            pass
        return None

    @database_sync_to_async
    def _check_camera_token(self, camera_id, token):
        from .models import Camera
        try:
            camera = Camera.objects.get(camera_id=camera_id)
            if camera.token and hmac.compare_digest(token, camera.token):
                return True
        except Camera.DoesNotExist:
            pass
        return False
