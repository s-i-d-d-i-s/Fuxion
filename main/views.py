import math
import re
import tempfile
import zipfile

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .genuine_mix import GenuineMixError, save_genuine_mix
from .models import Playlist, PlaylistExclusion, Track


def playlist_list(request):
    query = request.GET.get("q", "").strip()
    playlists = Playlist.objects.annotate(
        track_count=Count(
            "tracks",
            filter=Q(
                tracks__rights_confirmed=True,
                tracks__availability=Track.Availability.AVAILABLE,
            ),
            distinct=True,
        )
    )
    if query:
        playlists = playlists.filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
    return render(
        request,
        "main/playlist_list.html",
        {"playlists": playlists, "query": query},
    )


def playlist_detail(request, slug):
    playlist = get_object_or_404(Playlist, slug=slug)
    tracks = playlist.tracks.filter(rights_confirmed=True).order_by(
        "sort_order", "pk"
    )
    return render(
        request,
        "main/playlist_detail.html",
        {"playlist": playlist, "tracks": tracks, "can_manage": request.user.is_staff},
    )


def download_playlist(request, slug):
    playlist = get_object_or_404(Playlist, slug=slug)
    tracks = list(
        playlist.tracks.filter(
            rights_confirmed=True,
            availability=Track.Availability.AVAILABLE,
        )
        .exclude(audio_file="")
        .order_by("sort_order", "pk")
    )
    if not tracks:
        messages.warning(request, "This playlist has no tracks to download yet.")
        return redirect("playlist_detail", slug=playlist.slug)

    archive = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_STORED) as bundle:
        for index, track in enumerate(tracks, start=1):
            safe_title = slugify(track.title)[:100] or f"track-{index}"
            archive_name = f"{index:02d}-{safe_title}{track.file_extension}"
            with track.audio_file.open("rb") as audio_file:
                with bundle.open(archive_name, mode="w") as archive_file:
                    while chunk := audio_file.read(1024 * 1024):
                        archive_file.write(chunk)

    archive.seek(0)
    filename = re.sub(r"[^a-zA-Z0-9_-]+", "-", playlist.slug).strip("-")
    return FileResponse(
        archive,
        as_attachment=True,
        filename=f"{filename or 'playlist'}.zip",
        content_type="application/zip",
    )


@staff_member_required
def save_genuine_mix_settings(request, slug):
    if request.method != "POST":
        return HttpResponseNotAllowed(("POST",))

    playlist = get_object_or_404(Playlist, slug=slug)
    error = _apply_genuine_mix_settings(playlist, request.POST)
    if error:
        messages.error(request, error)
        return redirect("playlist_detail", slug=playlist.slug)
    messages.success(request, "Genuine Mix settings saved.")
    return redirect("playlist_detail", slug=playlist.slug)


def _apply_genuine_mix_settings(playlist, data):
    tracks = list(
        playlist.tracks.filter(rights_confirmed=True).order_by("sort_order", "pk")
    )
    if set(data.getlist("track_ids")) != {str(track.pk) for track in tracks}:
        return "The track settings could not be verified. Please try again."

    crossfade = _mix_seconds(data.get("crossfade_seconds", ""))
    if crossfade is None or crossfade > 30:
        return "Crossfade must be between 0 and 30 seconds."

    updates = []
    for track in tracks:
        start = _mix_seconds(data.get(f"start_{track.pk}", ""))
        end_value = data.get(f"end_{track.pk}", "").strip()
        end = _mix_seconds(end_value) if end_value else None
        if start is None or (end_value and end is None) or (
            end is not None and end <= start
        ):
            return (
                f"Enter valid trim points for “{track.title}”; "
                "the end must be later than the start."
            )
        updates.append(
            (
                track,
                track.availability == Track.Availability.AVAILABLE
                and bool(track.audio_file)
                and data.get(f"include_{track.pk}") == "on",
                start,
                end,
            )
        )

    with transaction.atomic():
        playlist.crossfade_seconds = crossfade
        playlist.save(update_fields=("crossfade_seconds",))
        for track, include, start, end in updates:
            track.include_in_mix = include
            track.mix_start_seconds = start
            track.mix_end_seconds = end
            track.save(
                update_fields=("include_in_mix", "mix_start_seconds", "mix_end_seconds")
            )
    return None


@staff_member_required
def generate_genuine_mix(request, slug):
    if request.method != "POST":
        return HttpResponseNotAllowed(("POST",))

    playlist = get_object_or_404(Playlist, slug=slug)
    if not playlist.rights_confirmed:
        return JsonResponse(
            {"error": "Confirm music rights in Studio before generating a mix."},
            status=400,
        )
    error = _apply_genuine_mix_settings(playlist, request.POST)
    if error:
        return JsonResponse({"error": error}, status=400)
    playlist.refresh_from_db()
    try:
        save_genuine_mix(playlist)
    except GenuineMixError as error:
        return JsonResponse({"error": str(error)}, status=400)
    return JsonResponse(
        {
            "download_url": reverse(
                "download_genuine_mix", kwargs={"slug": playlist.slug}
            )
        }
    )


def download_genuine_mix(request, slug):
    playlist = get_object_or_404(Playlist, slug=slug)
    if not playlist.mix_file:
        return redirect("playlist_detail", slug=playlist.slug)
    return FileResponse(
        playlist.mix_file.open("rb"),
        as_attachment=True,
        filename=f"{playlist.slug}-genuine-mix.mp3",
        content_type="audio/mpeg",
    )


def _mix_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


@staff_member_required
def request_playlist_sync(request, slug):
    if request.method != "POST":
        return redirect("playlist_detail", slug=slug)

    playlist = get_object_or_404(Playlist, slug=slug)
    if not playlist.rights_confirmed:
        messages.error(
            request,
            "Confirm that you own or may download this playlist in Studio before syncing.",
        )
        return redirect("playlist_detail", slug=playlist.slug)

    updated = Playlist.objects.filter(pk=playlist.pk, sync_status__in=("idle", "failed")).update(
        sync_status="pending",
        sync_error="",
    )
    if updated:
        messages.success(
            request, "Sync queued. New tracks will appear when the sync worker finishes."
        )
    else:
        messages.info(request, "This playlist is already queued or syncing.")
    return redirect("playlist_detail", slug=playlist.slug)


@staff_member_required
def recover_playlist_sync(request, slug):
    if request.method != "POST":
        return redirect("playlist_detail", slug=slug)

    playlist = get_object_or_404(Playlist, slug=slug)
    updated = Playlist.objects.filter(
        pk=playlist.pk, sync_status="syncing"
    ).update(sync_status="idle", sync_error="")
    if updated:
        messages.success(
            request,
            "The stuck sync status was cleared. You can queue another sync if needed.",
        )
    else:
        messages.info(request, "This playlist is not marked as syncing.")
    return redirect("playlist_detail", slug=playlist.slug)


@staff_member_required
def move_track(request, slug, track_id, direction):
    if request.method != "POST":
        return redirect("playlist_detail", slug=slug)
    if direction not in {"up", "down"}:
        messages.error(request, "Invalid track order action.")
        return redirect("playlist_detail", slug=slug)

    playlist = get_object_or_404(Playlist, slug=slug)
    reordered = False
    with transaction.atomic():
        tracks = list(
            Track.objects.select_for_update()
            .filter(playlist=playlist, rights_confirmed=True)
            .order_by("sort_order", "pk")
        )
        index = next(
            (i for i, track in enumerate(tracks) if track.pk == track_id), None
        )
        if index is None:
            return redirect("playlist_detail", slug=playlist.slug)

        for position, item in enumerate(tracks):
            item.sort_order = position
        Track.objects.bulk_update(tracks, ("sort_order",))

        other_index = index - 1 if direction == "up" else index + 1
        if 0 <= other_index < len(tracks):
            track, other = tracks[index], tracks[other_index]
            track.sort_order, other.sort_order = other.sort_order, track.sort_order
            Track.objects.bulk_update((track, other), ("sort_order",))
            reordered = True

    if reordered:
        playlist.invalidate_mix()
    return redirect("playlist_detail", slug=playlist.slug)


@staff_member_required
def remove_track(request, slug, track_id):
    if request.method != "POST":
        return redirect("playlist_detail", slug=slug)

    playlist = get_object_or_404(Playlist, slug=slug)
    with transaction.atomic():
        track = get_object_or_404(
            Track.objects.select_for_update(),
            pk=track_id,
            playlist=playlist,
        )
        if track.source_video_id:
            PlaylistExclusion.objects.get_or_create(
                playlist=playlist, source_video_id=track.source_video_id
            )
        track.audio_file.delete(save=False)
        track.delete()
        Playlist.objects.filter(pk=playlist.pk).update(sync_error="")

    messages.success(request, f"Removed “{track.title}” from this playlist.")
    return redirect("playlist_detail", slug=playlist.slug)
