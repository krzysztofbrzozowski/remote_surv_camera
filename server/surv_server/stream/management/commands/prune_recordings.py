"""Prune recordings that exceed per-camera storage quota.

Deletes oldest recordings first until camera is within max_storage_gb.
Run periodically via cron or systemd timer:
  python manage.py prune_recordings
"""
import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from ...models import Camera

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete oldest recordings when a camera exceeds its storage quota"

    def handle(self, *args, **options):
        cameras = Camera.objects.all()
        total_pruned = 0

        for camera in cameras:
            max_bytes = int(camera.max_storage_gb * 1024 ** 3)
            used = camera.storage_used_bytes

            if used <= max_bytes:
                continue

            recordings = camera.recordings.order_by("started_at")
            for rec in recordings:
                if used <= max_bytes:
                    break
                rec_path = Path(settings.RECORDINGS_DIR) / camera.camera_id / rec.filename
                mp4_path = rec_path.with_suffix(".mp4")
                for p in (rec_path, mp4_path):
                    if p.exists():
                        p.unlink()
                used -= rec.size_bytes
                rec.delete()
                total_pruned += 1
                logger.info(f"Pruned {rec.filename} from {camera.camera_id}")

        self.stdout.write(f"Pruned {total_pruned} recordings")
