from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.text import slugify


class Playlist(models.Model):
    class Category(models.TextChoices):
        POP = "pop", "Pop"
        ELECTRONIC = "electronic", "Electronic"
        ROCK = "rock", "Rock"
        RNB = "rnb", "R&B"
        HIP_HOP = "hiphop", "Hip-hop"
        OTHER = "other", "Other"

    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    source_url = models.URLField("playlist link", max_length=500)
    description = models.TextField(blank=True, max_length=600)
    category = models.CharField(
        max_length=16, choices=Category.choices, default=Category.POP
    )
    featured = models.BooleanField(default=False)
    rights_confirmed = models.BooleanField(
        default=False,
        help_text="Confirm you own the music or are authorized to download it.",
    )
    sync_status = models.CharField(
        max_length=12,
        choices=(
            ("idle", "Ready"),
            ("pending", "Queued"),
            ("syncing", "Syncing"),
            ("failed", "Failed"),
        ),
        default="idle",
    )
    sync_error = models.TextField(blank=True)
    last_synced = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-featured", "title")
        verbose_name = "playlist"
        verbose_name_plural = "playlists"

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        try:
            parsed = urlparse(self.source_url or "")
        except ValueError:
            parsed = None
        if (
            not parsed
            or parsed.scheme != "https"
            or parsed.hostname
            not in {"youtube.com", "www.youtube.com", "music.youtube.com"}
            or not parse_qs(parsed.query).get("list")
        ):
            raise ValidationError(
                {"source_url": "Enter a YouTube playlist URL with a playlist ID."}
            )

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title)[:170] or "playlist"
            candidate = base_slug
            suffix = 2
            while Playlist.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base_slug}-{suffix}"
                suffix += 1
            self.slug = candidate
        super().save(*args, **kwargs)


def validate_audio_size(file):
    max_size = 200 * 1024 * 1024
    if file.size > max_size:
        raise ValidationError("Audio files must be 200 MB or smaller.")


class Track(models.Model):
    class Availability(models.TextChoices):
        AVAILABLE = "available", "Available"
        UNAVAILABLE = "unavailable", "Unavailable"

    playlist = models.ForeignKey(
        Playlist, related_name="tracks", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=160)
    source_video_id = models.CharField(max_length=32, blank=True)
    availability = models.CharField(
        max_length=12,
        choices=Availability.choices,
        default=Availability.AVAILABLE,
    )
    unavailable_reason = models.TextField(blank=True)
    audio_file = models.FileField(
        upload_to="playlist_tracks/%Y/%m/",
        blank=True,
        validators=[
            FileExtensionValidator(
                allowed_extensions=("mp3", "m4a", "flac", "ogg", "opus", "wav", "aac")
            ),
            validate_audio_size,
        ],
    )
    rights_confirmed = models.BooleanField(
        default=False,
        help_text="Confirm you own this audio or are authorized to distribute it.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("sort_order", "title")
        verbose_name = "audio track"
        verbose_name_plural = "audio tracks"
        constraints = [
            models.UniqueConstraint(
                fields=("playlist", "source_video_id"),
                condition=~models.Q(source_video_id=""),
                name="unique_source_video_per_playlist",
            )
        ]

    def __str__(self):
        return f"{self.title} — {self.playlist.title}"

    def clean(self):
        super().clean()
        if self.availability == self.Availability.AVAILABLE and not self.audio_file:
            raise ValidationError(
                {"audio_file": "Available tracks must have an audio file."}
            )

    @property
    def file_extension(self):
        return Path(self.audio_file.name).suffix.lower()


class PlaylistExclusion(models.Model):
    playlist = models.ForeignKey(
        Playlist, related_name="exclusions", on_delete=models.CASCADE
    )
    source_video_id = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("playlist", "source_video_id"),
                name="unique_excluded_video_per_playlist",
            )
        ]

    def __str__(self):
        return f"{self.source_video_id} excluded from {self.playlist}"
