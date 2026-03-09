import os
import sys
import django
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "surv_server.settings")
django.setup()

from surv_server.stream.routing import websocket_urlpatterns
from surv_server.stream.middleware import TokenAuthMiddleware

# Start daily logger (RX/TX tracking + daily log files)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from daily_logger import DailyLogger
    _daily_logger = DailyLogger()
    _daily_logger.start()
    _server_log = _daily_logger.get_app_logger()
    _server_log.info("Server daily logger started")
except Exception as _e:
    import logging
    logging.getLogger("server").warning(f"Daily logger unavailable: {_e}")

# Start background stats collector (CPU, memory, network — runs 24/7)
try:
    from surv_server.stream.views import start_background_stats_collector
    start_background_stats_collector()
except Exception as _e:
    import logging
    logging.getLogger("server").warning(f"Background stats collector unavailable: {_e}")

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        TokenAuthMiddleware(
            URLRouter(websocket_urlpatterns)
        )
    ),
})
