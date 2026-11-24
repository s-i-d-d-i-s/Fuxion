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

from .genuine_mix import _mix_filter
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
        }
        defaults.update(kwargs)
        return Playlist.objects.create(**defaults)

    def make_track(
        self,
        playlist,
        title="Night Lights",
        content=b"audio",
        source_video_id="",
        sort_order=0,
    ):
        return Track.objects.create(
            playlist=playlist,
            title=title,
            source_video_id=source_video_id,
            audio_file=SimpleUploadedFile(f"{title}.mp3", content),
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

    def test_studio_can_add_a_youtube_playlist(self):
        self.client.force_login(self.make_staff_user())
        response = self.client.post(
            reverse("admin:main_playlist_add"),
            {
                "title": "Studio Playlist",
                "slug": "",
                "source_url": "https://music.youtube.com/playlist?list=PLabcdefghijk",
                "crossfade_seconds": "5",
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

    def test_catalog_searches_titles_and_counts_available_tracks(self):
        playlist = self.make_playlist()
        self.make_track(playlist)
        self.make_track(playlist, title="Another track")

        response = self.client.get(reverse("playlist_list"), {"q": "Late Night"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Late Night Drive")
        self.assertEqual(response.context["playlists"][0].track_count, 2)

    def test_detail_and_zip_follow_the_same_order(self):
        playlist = self.make_playlist()
        self.make_track(playlist, title="Second track", content=b"second", sort_order=1)
        self.make_track(playlist, title="First track", content=b"first", sort_order=0)
        self.make_track(playlist, title="Third track", content=b"third", sort_order=2)

        detail = self.client.get(
            reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        response = self.client.get(
            reverse("download_playlist", kwargs={"slug": playlist.slug})
        )

        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "data-play-track")
        self.assertContains(detail, "First track")
        self.assertContains(detail, "Third track")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(b"".join(response.streaming_content))) as archive:
            self.assertEqual(
                archive.namelist(),
                ["01-first-track.mp3", "02-second-track.mp3", "03-third-track.mp3"],
            )
            self.assertEqual(archive.read("01-first-track.mp3"), b"first")
            self.assertEqual(archive.read("02-second-track.mp3"), b"second")
            self.assertEqual(archive.read("03-third-track.mp3"), b"third")

    def test_genuine_mix_settings_are_saved_for_each_track(self):
        playlist = self.make_playlist()
        track = self.make_track(playlist)
        self.client.force_login(self.make_staff_user())

        response = self.client.post(
            reverse("save_genuine_mix_settings", kwargs={"slug": playlist.slug}),
            {
                "crossfade_seconds": "7.5",
                "track_ids": str(track.pk),
                f"include_{track.pk}": "on",
                f"start_{track.pk}": "2.5",
                f"end_{track.pk}": "18",
            },
        )

        self.assertEqual(response.status_code, 302)
        playlist.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual(playlist.crossfade_seconds, 7.5)
        self.assertTrue(track.include_in_mix)
        self.assertEqual(track.mix_start_seconds, 2.5)
        self.assertEqual(track.mix_end_seconds, 18)

    def test_unavailable_tracks_are_disabled_and_bulk_selection_controls_exist(self):
        playlist = self.make_playlist()
        unavailable = Track.objects.create(
            playlist=playlist,
            title="Unavailable song",
            availability=Track.Availability.UNAVAILABLE,
        )
        self.client.force_login(self.make_staff_user())

        response = self.client.get(
            reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )

        self.assertContains(response, "Check all")
        self.assertContains(response, "Uncheck all")
        self.assertContains(response, f'name="include_{unavailable.pk}" disabled')
        self.assertNotContains(response, f'name="include_{unavailable.pk}" checked')

    def test_genuine_mix_generation_saves_settings_before_processing(self):
        playlist = self.make_playlist()
        track = self.make_track(playlist)
        self.client.force_login(self.make_staff_user())

        with patch("main.views.save_genuine_mix") as generate:
            response = self.client.post(
                reverse("generate_genuine_mix", kwargs={"slug": playlist.slug}),
                {
                    "crossfade_seconds": "4",
                    "track_ids": str(track.pk),
                    f"include_{track.pk}": "on",
                    f"start_{track.pk}": "1",
                    f"end_{track.pk}": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["download_url"],
            reverse("download_genuine_mix", kwargs={"slug": playlist.slug}),
        )
        playlist.refresh_from_db()
        track.refresh_from_db()
        self.assertEqual(playlist.crossfade_seconds, 4)
        self.assertEqual(track.mix_start_seconds, 1)
        generate.assert_called_once()

    def test_genuine_mix_filter_crossfades_in_track_order(self):
        self.assertEqual(
            _mix_filter(3, 5),
            "[0:a][1:a]acrossfade=d=5:c1=tri:c2=tri[mix1];"
            "[mix1][2:a]acrossfade=d=5:c1=tri:c2=tri[out]",
        )
        self.assertEqual(_mix_filter(2, 0), "[0:a][1:a]concat=n=2:v=0:a=1[out]")

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

    def test_sync_requires_staff_but_not_rights_confirmation(self):
        playlist = self.make_playlist()
        url = reverse("request_playlist_sync", kwargs={"slug": playlist.slug})

        anonymous_response = self.client.post(url)
        self.assertEqual(anonymous_response.status_code, 302)

        self.client.force_login(self.make_staff_user())
        response = self.client.post(url)
        playlist.refresh_from_db()

        self.assertRedirects(
            response, reverse("playlist_detail", kwargs={"slug": playlist.slug})
        )
        self.assertEqual(playlist.sync_status, "pending")

    def test_sync_request_queues_once(self):
        playlist = self.make_playlist()
        self.client.force_login(self.make_staff_user())
        url = reverse("request_playlist_sync", kwargs={"slug": playlist.slug})

        self.client.post(url)
        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "pending")

        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Playlist.objects.get(pk=playlist.pk).sync_status, "pending")

    def test_staff_can_clear_a_stuck_sync_status(self):
        playlist = self.make_playlist(sync_status="syncing")
        self.client.force_login(self.make_staff_user())

        response = self.client.post(
            reverse("recover_playlist_sync", kwargs={"slug": playlist.slug})
        )

        self.assertEqual(response.status_code, 302)
        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "idle")
        self.assertEqual(playlist.sync_error, "")

    def test_recover_sync_does_not_change_other_sync_states(self):
        playlist = self.make_playlist(sync_status="pending")
        self.client.force_login(self.make_staff_user())

        self.client.post(
            reverse("recover_playlist_sync", kwargs={"slug": playlist.slug})
        )

        playlist.refresh_from_db()
        self.assertEqual(playlist.sync_status, "pending")

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
        playlist = self.make_playlist()
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
        playlist = self.make_playlist(sync_status="syncing")
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
        playlist = self.make_playlist(sync_status="syncing")

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
        playlist = self.make_playlist(sync_status="syncing")
        track = Track.objects.create(
            playlist=playlist,
            title="Unavailable song",
            source_video_id="Recovered123",
            availability=Track.Availability.UNAVAILABLE,
            unavailable_reason="Temporary failure",
            sort_order=4,
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
        playlist = self.make_playlist(sync_status="pending")
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
