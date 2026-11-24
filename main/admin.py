from django.contrib import admin

from .models import Playlist, Track


class TrackInline(admin.TabularInline):
    model = Track
    extra = 0
    can_delete = False
    fields = (
        "title",
        "source_video_id",
        "availability",
        "unavailable_reason",
        "audio_file",
        "sort_order",
        "include_in_mix",
        "mix_start_seconds",
        "mix_end_seconds",
    )
    readonly_fields = ("source_video_id", "availability", "unavailable_reason")
    show_change_link = True


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = ("title", "sync_status", "track_count", "created_at")
    list_filter = ("featured",)
    search_fields = ("title", "source_url")
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ("created_at", "sync_status", "last_synced", "sync_error")
    inlines = (TrackInline,)
    fieldsets = (
        (None, {"fields": ("title", "slug", "source_url", "featured")}),
        ("Genuine Mix", {"fields": ("crossfade_seconds",)}),
        (
            "Sync status",
            {"fields": ("sync_status", "last_synced", "sync_error", "created_at")},
        ),
    )

    @admin.display(description="Available tracks")
    def track_count(self, obj):
        return obj.tracks.filter(
            availability=Track.Availability.AVAILABLE,
        ).count()


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "playlist",
        "availability",
        "source_video_id",
        "sort_order",
        "include_in_mix",
    )
    list_filter = ("availability", "include_in_mix", "playlist")
    search_fields = ("title", "playlist__title")
    list_editable = ("sort_order", "include_in_mix")
    readonly_fields = ("source_video_id", "availability", "unavailable_reason")

    def has_delete_permission(self, request, obj=None):
        return False
