from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("main", "0003_track_availability_track_unavailable_reason_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="playlist",
            name="crossfade_seconds",
            field=models.FloatField(
                default=5,
                help_text="Crossfade duration between tracks in the Genuine Mix (0–30 seconds).",
                validators=[
                    MinValueValidator(0),
                    MaxValueValidator(30),
                ],
            ),
        ),
        migrations.AddField(
            model_name="playlist",
            name="mix_file",
            field=models.FileField(
                blank=True,
                upload_to="playlist_mixes/%Y/%m/",
            ),
        ),
        migrations.AddField(
            model_name="track",
            name="include_in_mix",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="track",
            name="mix_start_seconds",
            field=models.FloatField(
                default=0,
                help_text="Trim start in seconds, after silence is removed.",
                validators=[MinValueValidator(0)],
            ),
        ),
        migrations.AddField(
            model_name="track",
            name="mix_end_seconds",
            field=models.FloatField(
                blank=True,
                help_text="Optional trim end in seconds, after silence is removed.",
                null=True,
                validators=[MinValueValidator(0)],
            ),
        ),
    ]
