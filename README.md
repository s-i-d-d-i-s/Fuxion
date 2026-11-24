# Fuxion

Fuxion is a personal YouTube Music library. It stores playlist audio locally,
keeps your track order, streams it in the browser, and downloads playlists as ZIPs.
Studio users can also configure a Genuine Mix: choose included tracks, set trim
points after edge-silence removal, set a 0–30 second crossfade, and generate one MP3
in the playlist's displayed order.
Sync is additive: it never removes or reorders existing tracks. Removing a track
in Studio deletes its local file and keeps it excluded from later syncs.
Unavailable playlist entries are kept in the tracklist with their status and error;
the worker continues to later tracks, and the next sync retries unavailable tracks.

Only sync playlists containing music you own or are authorized to download.
Playlist sync supports public YouTube Music/YouTube playlists.

## Install

Install Python 3.10 or later and FFmpeg (both `ffmpeg` and `ffprobe` must be
available). For example:

```shell
# Debian / Ubuntu
sudo apt install ffmpeg

# macOS with Homebrew
brew install ffmpeg
```

On Windows, install an FFmpeg build and add its `bin` folder to `PATH`. If FFmpeg
isn't on `PATH`, set `YTDLP_FFMPEG_LOCATION` to its `bin` folder or the
`ffmpeg` executable. Restart the app after changing environment variables.

Create and activate a virtual environment (`.venv\Scripts\activate` on Windows
or `source .venv/bin/activate` on macOS/Linux), then run:

```shell
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

In a second terminal, activate the same environment and start the sync worker:

```shell
python manage.py sync_playlists
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Sign in at
[http://127.0.0.1:8000/admin/](http://127.0.0.1:8000/admin/), add a playlist,
and confirm that you own or may download its music. Open the playlist in Fuxion
and select **Sync playlist**. Keep the worker running while syncs are queued.
To create a Genuine Mix, configure inclusion and trim points on the playlist page,
save the settings, and select **Generate Genuine Mix**. FFmpeg processes the mix
during that request; the page reports that it is working, then starts the MP3
download when it finishes. Mix creation is limited to 100 tracks and 2 GB of
source audio per request.

## Production

Set `DJANGO_DEBUG=0`, provide a unique `DJANGO_SECRET_KEY`, and set
`DJANGO_ALLOWED_HOSTS` to comma-separated hostnames. Use a production web server,
database, persistent media storage, and a supervised `sync_playlists` worker.
Serve `MEDIA_ROOT` through correctly configured web-server or object storage;
downloads and streaming depend on those media files being available.
