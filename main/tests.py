import io
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Playlist, PlaylistExclusion, Track


class PlaylistCatalogTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

    def make_playlist(self, **kwargs):
        defaults = {
            "title": "Late Night Drive",
            "source_url": "https://music.youtube.com/playlist?list=PLabcdefghijk",
            "category": Playlist.Category.POP,
        }
        defaults.update(kwargs)
        return Playlist.objects.create(**defaults)

    def make_track(
        self,
        playlist,
        title="Night Lights",
        cleared=True,
        content=b"audio",
        source_video_id="",
        sort_order=0,
    ):
        return Track.objects.create(
            playlist=playlist,
            title=title,
            source_video_id=source_video_id,
            audio_file=SimpleUploadedFile(f"{title}.mp3", content),
            rights_confirmed=cleared,
            sort_order=sort_order,
        )

    def make_staff_user(self):
        return get_user_model().objects.create_superuser(
            username="studio",
            email="studio@example.invalid",
            password="test-password-123",
        )

    def test_playlist_slug_is_generated_and_unique(self):
        first = self.make_playlist()
        second = self.make_playlist()
        self.assertEqual(first.slug, "late-night-drive")
        self.assertEqual(second.slug, "late-night-drive-2")

    def test_playlist_requires_youtube_playlist_url(self):
        playlist = self.make_playlist(source_url="https://example.com/audio")
        with self.assertRaises(ValidationError):
            playlist.full_clean()

    def test_studio_can_add_a_rights_confirmed_youtube_playlist(self):
        self.client.force_login(self.make_staff_user())
        response = self.client.post(
            reverse("admin:main_playlist_add"),
            {
                "title": "Studio Playlist",
                "slug": "",
                "source_url": "https://music.youtube.com/playlist?list=PLabcdefghijk",
                "description": "",
                "category": Playlist.Category.POP,
                "rights_confirmed": "on",
                "tracks-TOTAL_FORMS": "0",
                "tracks-INITIAL_FORMS": "0",
                "tracks-MIN_NUM_FORMS": "0",
                "tracks-MAX_NUM_FORMS": "1000",
                "_save": "Save",
            },
        )

        self.assertEqual(response.status_code, 302)
        playlist = Playlist.objects.get(title="Studio Playlist")
        self.assertEqual(playlist.slug, "studio-playlist")
        self.assertTrue(playlist.rights_confirmed)

    def test_catalog_search_filters_and_counts_playable_tracks(self):
        playlist = self.make_playlist(description="A mellow night mix")
        self.make_track(playlist)
        self.make_track(playlist, title="Uncleared track", cleared=False)

        response = self.client.get(reverse("playlist_list"), {"q": "mellow"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Late Night Drive")
        self.assertEqual(response.context["playlists"][0].track_count, 1)

    def test_detail_and_zip_follow_the_same_order(self):
        playlist = self.make_playlist()
        self.make_track(playlist, title="Second track", content=b"second", sort_order=1)
        self.make_track(playlist, title="First track", content=b"first", sort_order=0)
        self.make_track(playlist, title="Not cleared", cleared=False, content=b"private")

        detail = self.client.get(
            reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        response = self.client.get(
            reverse("download_playlist", kwargs={"slug": playlist.slug})
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "data-play-track")
        self.assertContains(detail, "First track")
        self.assertNotContains(detail, "Not cleared")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))) as archive:
            self.assertEqual(
                archive.namelist(), ["01-first-track.mp3", "02-second-track.mp3"]
            )
            self.assertEqual(archive.read("01-first-track.mp3"), b"first")
            self.assertEqual(archive.read("02-second-track.mp3"), b"second")

    def test_unavailable_tracks_are_visible_but_not_played_or_zipped(self):
        playlist = self.make_playlist()
        available = self.make_track(playlist, title="Playable")
        unavailable = Track.objects.create(
            playlist=playlist,
            title="Region locked",
            source_video_id="Locked12345",
            availability=Track.Availability.UNAVAILABLE,
            unavailable_reason="This video is not available in your country.",
            sort_order=1,
            rights_confirmed=True,
        )

        detail = self.client.get(
            reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        response = self.client.get(
            reverse("download_playlist", kwargs={"slug": playlist.slug})
        )

        self.assertContains(detail, "Region locked")
        self.assertContains(detail, "not available in your country")
        self.assertContains(detail, "Unavailable")
        self.assertContains(detail, f'data-src="{available.audio_file.url}"')
        self.assertNotContains(
            detail,
            f'data-src="{unavailable.audio_file.url if unavailable.audio_file else ""}"',
        )
        with zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))) as archive:
            self.assertEqual(archive.namelist(), ["01-playable.mp3"])

    def test_playlist_without_tracks_redirects_with_message(self):
        playlist = self.make_playlist()
        response = self.client.get(
            reverse("download_playlist", kwargs={"slug": playlist.slug})
        )
        self.assertRedirects(
            response, reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        self.assertEqual(len(list(response.wsgi_request._messages)), 1)

    def test_unknown_playlist_returns_404(self):
        response = self.client.get(
            reverse("playlist_detail", kwargs={"slug": "not-a-playlist"})
        )
        self.assertEqual(response.status_code, 404)

    def test_sync_requires_staff_and_explicit_rights_confirmation(self):
        playlist = self.make_playlist(rights_confirmed=True)
        url = reverse("request_playlist_sync", kwargs={"slug": playlist.slug})

        anonymous_response = self.client.post(url)
        self.assertEqual(anonymous_response.status_code, 302)

        self.client.force_login(self.make_staff_user())
        playlist.rights_confirmed = False
        playlist.save()
        response = self.client.post(url)
        playlist.refresh_from_db()

        self.assertRedirects(
            response, reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        self.assertEqual(playlist.sync_status, "idle")

    def test_sync_request_queues_once(self):
        playlist = self.make_playlist(rights_confirmed=True)
        self.client.force_login(self.make_staff_user())
        url = reverse("request_playlist_sync", kwargs={"slug": playlist.slug})

        self.client.post(url)
        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "pending")

        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Playlist.objects.get(pk=playlist.pk).sync_status, "pending")

    def test_reorder_controls_persist_track_order(self):
        playlist = self.make_playlist()
        first = self.make_track(playlist, title="First", sort_order=0)
        second = self.make_track(playlist, title="Second", sort_order=1)
        third = self.make_track(playlist, title="Third", sort_order=2)
        self.client.force_login(self.make_staff_user())

        response = self.client.post(
            reverse(
                "move_track",
                kwargs={"slug": playlist.slug, "track_id": third.pk, "direction": "up"},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            list(
                Track.objects.filter(playlist=playlist).values_list("pk", flat=True)
            ),
            [first.pk, third.pk, second.pk],
        )

    def test_removal_deletes_audio_and_excludes_track_from_future_sync(self):
        playlist = self.make_playlist(rights_confirmed=True)
        track = self.make_track(playlist, source_video_id="Removed123")
        stored_path = Path(track.audio_file.path)
        self.client.force_login(self.make_staff_user())

        response = self.client.post(
            reverse(
                "remove_track",
                kwargs={"slug": playlist.slug, "track_id": track.pk},
            )
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(stored_path.exists())
        self.assertTrue(
            PlaylistExclusion.objects.filter(
                playlist=playlist, source_video_id="Removed123"
            ).exists()
        )

    def test_sync_adds_only_new_tracks_and_preserves_existing_order(self):
        playlist = self.make_playlist(rights_confirmed=True, sync_status="syncing")
        existing = self.make_track(
            playlist,
            title="Already here",
            source_video_id="Existing123",
            sort_order=7,
        )
        PlaylistExclusion.objects.create(
            playlist=playlist, source_video_id="Removed123"
        )

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                if not download:
                    return {
                        "entries": [
                            {"id": "Existing123", "title": "Already here"},
                            {"id": "NewVideo123", "title": "New song"},
                            {"id": "Removed123", "title": "Removed song"},
                        ]
                    }
                video_id = parse_qs(urlparse(url).query)["v"][0]
                output = self.options["outtmpl"].replace(
                    "%(id)s.%(ext)s", f"{video_id}.mp3"
                )
                Path(output).write_bytes(b"new audio")
                return {"id": video_id, "title": "New song"}

        with (
            patch("main.services.find_ffmpeg_location", return_value="/usr/bin"),
            patch("main.services.YoutubeDL", FakeYoutubeDL),
        ):
            from .services import sync_playlist

            added = sync_playlist(playlist.pk)

        self.assertEqual(added, 1)
        self.assertEqual(
            list(
                Track.objects.filter(playlist=playlist)
                .order_by("sort_order")
                .values_list("source_video_id", flat=True)
            ),
            ["Existing123", "NewVideo123"],
        )
        existing.refresh_from_db()
        self.assertEqual(existing.sort_order, 7)
        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "idle")
        self.assertIsNotNone(playlist.last_synced)

    def test_sync_marks_unavailable_entries_and_continues_to_later_tracks(self):
        playlist = self.make_playlist(rights_confirmed=True, sync_status="syncing")

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                if not download:
                    return {
                        "entries": [
                            None,
                            {"id": "Locked12345", "title": "Region locked"},
                            {"id": "Locked12345", "title": "Region locked duplicate"},
                            {"id": "Playable123", "title": "Now playing"},
                        ]
                    }
                video_id = parse_qs(urlparse(url).query)["v"][0]
                if video_id == "Locked12345":
                    raise RuntimeError("This video is not available")
                output = self.options["outtmpl"].replace(
                    "%(id)s.%(ext)s", f"{video_id}.mp3"
                )
                Path(output).write_bytes(b"playable audio")
                return {"id": video_id, "title": "Now playing"}

        with (
            patch("main.services.find_ffmpeg_location", return_value="/usr/bin"),
            patch("main.services.YoutubeDL", FakeYoutubeDL),
            self.assertLogs("main.services", level="WARNING"),
        ):
            from .services import sync_playlist

            added = sync_playlist(playlist.pk)

        self.assertEqual(added, 1)
        tracks = list(
            Track.objects.filter(playlist=playlist).order_by("sort_order", "pk")
        )
        self.assertEqual(
            [track.source_video_id for track in tracks],
            ["unavailable-slot-1", "Locked12345", "Playable123"],
        )
        self.assertEqual(
            [track.availability for track in tracks],
            ["unavailable", "unavailable", "available"],
        )
        self.assertEqual(
            tracks[1].unavailable_reason, "This video is not available"
        )
        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "idle")

    def test_later_sync_retries_and_recovers_unavailable_track_in_place(self):
        playlist = self.make_playlist(rights_confirmed=True, sync_status="syncing")
        track = Track.objects.create(
            playlist=playlist,
            title="Unavailable song",
            source_video_id="Recovered123",
            availability=Track.Availability.UNAVAILABLE,
            unavailable_reason="Temporary failure",
            sort_order=4,
            rights_confirmed=True,
        )

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                if not download:
                    return {"entries": [{"id": "Recovered123", "title": "Recovered song"}]}
                output = self.options["outtmpl"].replace(
                    "%(id)s.%(ext)s", "Recovered123.mp3"
                )
                Path(output).write_bytes(b"recovered audio")
                return {"id": "Recovered123", "title": "Recovered song"}

        with (
            patch("main.services.find_ffmpeg_location", return_value="/usr/bin"),
            patch("main.services.YoutubeDL", FakeYoutubeDL),
        ):
            from .services import sync_playlist

            added = sync_playlist(playlist.pk)

        track.refresh_from_db()
        self.assertEqual(added, 1)
        self.assertEqual(Track.objects.filter(playlist=playlist).count(), 1)
        self.assertEqual(track.availability, Track.Availability.AVAILABLE)
        self.assertEqual(track.unavailable_reason, "")
        self.assertEqual(track.sort_order, 4)
        self.assertEqual(track.audio_file.read(), b"recovered audio")

    def test_sync_worker_marks_failed_sync_for_studio(self):
        playlist = self.make_playlist(rights_confirmed=True, sync_status="pending")
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch(
                "main.management.commands.sync_playlists.sync_playlist",
                side_effect=RuntimeError("FFmpeg missing"),
            ),
            self.assertLogs("main.management.commands.sync_playlists", level="ERROR"),
        ):
            call_command(
                "sync_playlists",
                "--once",
                verbosity=0,
                stdout=output,
                stderr=errors,
            )

        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "failed")
        self.assertEqual(playlist.sync_error, "FFmpeg missing")
        self.assertIn(
            f"Playlist {playlist.pk} sync failed: FFmpeg missing", errors.getvalue()
        )
