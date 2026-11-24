"""
URL configuration for fuxion project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path

from main import views

urlpatterns = [
    path("", views.playlist_list, name="playlist_list"),
    path("playlists/<slug:slug>/", views.playlist_detail, name="playlist_detail"),
    path(
        "playlists/<slug:slug>/download/",
        views.download_playlist,
        name="download_playlist",
    ),
    path(
        "playlists/<slug:slug>/genuine-mix/",
        views.save_genuine_mix_settings,
        name="save_genuine_mix_settings",
    ),
    path(
        "playlists/<slug:slug>/genuine-mix/generate/",
        views.generate_genuine_mix,
        name="generate_genuine_mix",
    ),
    path(
        "playlists/<slug:slug>/genuine-mix/download/",
        views.download_genuine_mix,
        name="download_genuine_mix",
    ),
    path(
        "playlists/<slug:slug>/sync/",
        views.request_playlist_sync,
        name="request_playlist_sync",
    ),
    path(
        "playlists/<slug:slug>/sync/recover/",
        views.recover_playlist_sync,
        name="recover_playlist_sync",
    ),
    path(
        "playlists/<slug:slug>/tracks/<int:track_id>/move/<str:direction>/",
        views.move_track,
        name="move_track",
    ),
    path(
        "playlists/<slug:slug>/tracks/<int:track_id>/remove/",
        views.remove_track,
        name="remove_track",
    ),
    path("admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
