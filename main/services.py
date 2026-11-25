import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.core.files import File
from django.db import IntegrityError, transaction
from django.utils import timezone
from yt_dlp import YoutubeDL

from .models import Playlist, PlaylistExclusion, Track


logger = logging.getLogger(__name__)


class PlaylistSyncError(Exception):
    pass


def find_ffmpeg_location():
    configured = os.environ.get("YTDLP_FFMPEG_LOCATION")
    if configured:
        configured = os.path.expandvars(os.path.expanduser(configured))
        if os.path.isdir(configured):
            ffmpeg = shutil.which("ffmpeg", path=configured)
            ffprobe = shutil.which("ffprobe", path=configured)
            location = configured
        else:
            ffmpeg = shutil.which(configured)
            search_path = os.pathsep.join(
                filter(None, (os.path.dirname(configured), os.environ.get("PATH", "")))
            )
            ffprobe = shutil.which("ffprobe", path=search_path)
            location = ffmpeg
    else:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        location = None

    if not ffmpeg or not ffprobe:
        raise PlaylistSyncError(
            "FFmpeg and ffprobe are required to prepare streamable MP3 files. "
            "Install FFmpeg and add its bin folder to PATH, or set "
            "YTDLP_FFMPEG_LOCATION to that folder or the ffmpeg executable."
        )
    if location:
        return location

    ffmpeg_dir = os.path.dirname(os.path.abspath(ffmpeg))
    ffprobe_dir = os.path.dirname(os.path.abspath(ffprobe))
    return ffmpeg_dir if os.path.normcase(ffmpeg_dir) == os.path.normcase(ffprobe_dir) else ffmpeg


def validate_playlist_url(url):
    try:
        parsed = urlparse(url)
    except ValueError as error:
        raise PlaylistSyncError(
            "Enter a valid YouTube Music playlist URL."
        ) from error
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in {"youtube.com", "www.youtube.com", "music.youtube.com"}
        or not parse_qs(parsed.query).get("list")
    ):
        raise PlaylistSyncError(
            "Enter a YouTube Music playlist URL containing a playlist ID."
        )


def sync_playlist(playlist_id):
    playlist = Playlist.objects.get(pk=playlist_id)
    validate_playlist_url(playlist.source_url)
    ffmpeg_location = find_ffmpeg_location()

    info_options = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "cookiesfrombrowser": ("brave",),
    }
    try:
        with YoutubeDL(info_options) as ydl:
            info = ydl.extract_info(playlist.source_url, download=False)
    except Exception as error:
        raise PlaylistSyncError(f"Could not read the YouTube playlist: {error}") from error

    if not info:
        raise PlaylistSyncError("YouTube returned no playlist information.")
    entries = info.get("entries")
    if entries is None:
        raise PlaylistSyncError("The link did not resolve to a YouTube playlist.")

    existing_tracks = {
        track.source_video_id: track
        for track in Track.objects.filter(playlist=playlist)
        .exclude(source_video_id="")
    }
    excluded_ids = set(
        PlaylistExclusion.objects.filter(playlist=playlist).values_list(
            "source_video_id", flat=True
        )
    )
    unavailable_slots = {
        int(track.source_video_id.removeprefix("unavailable-slot-")): track
        for track in existing_tracks.values()
        if track.source_video_id.startswith("unavailable-slot-")
        and track.source_video_id.removeprefix("unavailable-slot-").isdigit()
    }
    next_order = (
        Track.objects.filter(playlist=playlist).order_by("-sort_order")
        .values_list("sort_order", flat=True)
        .first()
        or 0
    )
    new_count = 0
    unavailable_count = 0
    seen_video_ids = set()

    with tempfile.TemporaryDirectory(prefix="fuxion-sync-") as work_dir:
        for position, entry in enumerate(entries, start=1):
            if not entry:
                placeholder_id = f"unavailable-slot-{position}"
                placeholder = _record_unavailable(
                    playlist,
                    unavailable_slots.get(position),
                    placeholder_id,
                    f"Unavailable track {position}",
                    "YouTube did not provide track details for this playlist entry.",
                    next_order,
                )
                existing_tracks[placeholder_id] = placeholder
                if position not in unavailable_slots:
                    next_order += 1
                unavailable_count += 1
                continue
            video_id = str(entry.get("id") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", video_id)
            ):
                placeholder_id = f"unavailable-slot-{position}"
                placeholder = _record_unavailable(
                    playlist,
                    unavailable_slots.get(position),
                    placeholder_id,
                    (entry.get("title") or f"Unavailable track {position}")[:160],
                    "YouTube did not provide a valid video ID for this playlist entry.",
                    next_order,
                )
                existing_tracks[placeholder_id] = placeholder
                if position not in unavailable_slots:
                    next_order += 1
                unavailable_count += 1
                continue
            if video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            track = existing_tracks.get(video_id)
            if track and track.availability == Track.Availability.AVAILABLE:
                continue
            if video_id in excluded_ids:
                continue
            if not track:
                track = unavailable_slots.get(position)
                if track:
                    old_video_id = track.source_video_id
                    track.source_video_id = video_id
                    track.title = (entry.get("title") or video_id)[:160]
                    track.save(update_fields=("source_video_id", "title"))
                    existing_tracks.pop(old_video_id, None)
                    existing_tracks[video_id] = track
                else:
                    track = None

            download_options = {
                "format": "bestaudio/best",
                "outtmpl": str(Path(work_dir) / "%(id)s.%(ext)s"),
                "ffmpeg_location": ffmpeg_location,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "cookiesfrombrowser": ("brave",),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    },
                    {"key": "FFmpegMetadata", "add_metadata": True},
                ],
            }
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with YoutubeDL(download_options) as ydl:
                    downloaded = ydl.extract_info(video_url, download=True)
            except Exception as error:
                title = (entry.get("title") or video_id)[:160]
                track = _record_unavailable(
                    playlist,
                    track,
                    video_id,
                    title,
                    _unavailable_reason(error),
                    next_order,
                )
                if video_id not in existing_tracks:
                    next_order += 1
                existing_tracks[video_id] = track
                unavailable_count += 1
                logger.warning(
                    "Track %s in playlist %s is unavailable: %s",
                    video_id,
                    playlist.pk,
                    error,
                )
                continue

            audio_path = Path(work_dir) / f"{video_id}.mp3"
            if (
                not downloaded
                or not audio_path.is_file()
                or audio_path.stat().st_size == 0
            ):
                track = _record_unavailable(
                    playlist,
                    track,
                    video_id,
                    (entry.get("title") or video_id)[:160],
                    "Audio was unavailable or could not be converted to MP3.",
                    next_order,
                )
                if video_id not in existing_tracks:
                    next_order += 1
                existing_tracks[video_id] = track
                unavailable_count += 1
                continue
            if audio_path.stat().st_size > 200 * 1024 * 1024:
                track = _record_unavailable(
                    playlist,
                    track,
                    video_id,
                    (entry.get("title") or video_id)[:160],
                    "The converted audio exceeds the 200 MB track limit.",
                    next_order,
                )
                if video_id not in existing_tracks:
                    next_order += 1
                existing_tracks[video_id] = track
                unavailable_count += 1
                continue

            title = (downloaded.get("title") or entry.get("title") or video_id)[:160]
            with transaction.atomic():
                if track is None:
                    track = Track(
                        playlist=playlist,
                        title=title,
                        source_video_id=video_id,
                        sort_order=next_order + 1,
                    )
                else:
                    track.title = title
                track.availability = Track.Availability.AVAILABLE
                track.unavailable_reason = ""
                try:
                    with audio_path.open("rb") as audio_file:
                        track.audio_file.save(
                            f"{video_id}.mp3", File(audio_file), save=False
                        )
                    track.save()
                except IntegrityError:
                    track.audio_file.delete(save=False)
                    continue
            if video_id not in existing_tracks:
                next_order += 1
            new_count += 1
            existing_tracks[video_id] = track

    Playlist.objects.filter(pk=playlist.pk).update(
        sync_status="idle", sync_error="", last_synced=timezone.now()
    )
    if unavailable_count:
        logger.info(
            "Playlist %s sync completed with %s unavailable track(s)",
            playlist.pk,
            unavailable_count,
        )
    return new_count


def _record_unavailable(playlist, track, video_id, title, reason, next_order):
    if track is None:
        track = Track(
            playlist=playlist,
            title=title,
            source_video_id=video_id,
            sort_order=next_order + 1,
        )
    else:
        track.title = title
    track.availability = Track.Availability.UNAVAILABLE
    track.unavailable_reason = reason[:1000]
    track.save()
    return track


def _unavailable_reason(error):
    message = str(error).strip().splitlines()
    return (message[0] if message and message[0] else "YouTube could not provide this track.")[:1000]
