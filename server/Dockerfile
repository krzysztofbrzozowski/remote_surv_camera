FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    iproute2 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -r -s /bin/false appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput 2>/dev/null || true

# Pre-create writable directories owned by appuser
RUN mkdir -p /app/data /app/recordings /app/staticfiles /app/logs \
    && chown -R appuser:appuser /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python manage.py migrate --noinput && python manage.py collectstatic --noinput 2>/dev/null || true && (while true; do sleep 3600; python manage.py prune_recordings 2>&1; done &) && daphne -b 0.0.0.0 -p 8000 --ping-interval 20 --ping-timeout 30 surv_server.asgi:application"]
