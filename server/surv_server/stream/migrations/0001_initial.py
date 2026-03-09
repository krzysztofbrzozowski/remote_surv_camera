from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Camera",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("camera_id", models.CharField(max_length=100, unique=True)),
                ("name", models.CharField(blank=True, max_length=200)),
                ("is_online", models.BooleanField(default=False)),
                ("last_seen", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["camera_id"]},
        ),
        migrations.CreateModel(
            name="Recording",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("filename", models.CharField(max_length=255, unique=True)),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("size_bytes", models.BigIntegerField(default=0)),
                ("frame_count", models.IntegerField(default=0)),
                ("camera", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recordings", to="stream.camera")),
            ],
            options={"ordering": ["-started_at"]},
        ),
    ]
