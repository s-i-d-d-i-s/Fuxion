import logging
import time

from django.core.management.base import BaseCommand
from django.db import transaction

from main.models import Playlist, Track
from main.services import sync_playlist


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process queued playlist syncs. Run this in a separate terminal."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process current sync requests, then exit.",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=2,
            help="Seconds to wait when no playlists are queued.",
        )

    def handle(self, *args, **options):
        if options["poll_seconds"] < 0.2:
            self.stderr.write(self.style.ERROR("--poll-seconds must be at least 0.2."))
            return

        self.stdout.write("Playlist sync worker is running.")
        while True:
            playlist_id = self._claim_next_playlist()
            if playlist_id is None:
                if options["once"]:
                    return
                time.sleep(options["poll_seconds"])
                continue

            try:
                added = sync_playlist(playlist_id)
            except Exception as error:
                Playlist.objects.filter(
                    pk=playlist_id, sync_status="syncing"
                ).update(sync_status="failed", sync_error=str(error)[:4000])
                logger.exception("Playlist %s sync failed", playlist_id)
                self.stderr.write(
                    self.style.ERROR(f"Playlist {playlist_id} sync failed: {error}")
                )
            else:
                unavailable = Track.objects.filter(
                    playlist_id=playlist_id,
                    availability=Track.Availability.UNAVAILABLE,
                ).count()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Playlist {playlist_id} synced; added {added} new track(s); "
                        f"{unavailable} track(s) unavailable."
                    )
                )

    @staticmethod
    def _claim_next_playlist():
        with transaction.atomic():
            playlist = (
                Playlist.objects.filter(sync_status="pending")
                .order_by("created_at", "pk")
                .first()
            )
            if not playlist:
                return None
            claimed = Playlist.objects.filter(
                pk=playlist.pk, sync_status="pending"
            ).update(sync_status="syncing", sync_error="")
            return playlist.pk if claimed else None
