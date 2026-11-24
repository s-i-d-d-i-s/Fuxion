import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.files import File

from .models import Track
from .services import PlaylistSyncError, find_ffmpeg_location


class GenuineMixError(Exception):
    pass


def build_genuine_mix(playlist, output_path):
    tracks = list(
        playlist.tracks.filter(
            availability=Track.Availability.AVAILABLE,
            include_in_mix=True,
        )
        .exclude(audio_file="")
        .order_by("sort_order", "pk")
    )
    if not tracks:
        raise GenuineMixError("Select at least one available track.")
    if len(tracks) > 100:
        raise GenuineMixError("A Genuine Mix can contain up to 100 tracks.")
    if sum(track.audio_file.size for track in tracks) > 2 * 1024 * 1024 * 1024:
        raise GenuineMixError("The selected tracks exceed the 2 GB mix input limit.")
    if (
        not math.isfinite(playlist.crossfade_seconds)
        or not 0 <= playlist.crossfade_seconds <= 30
    ):
        raise GenuineMixError("Crossfade duration must be between 0 and 30 seconds.")

    try:
        ffmpeg_location = find_ffmpeg_location()
    except PlaylistSyncError as error:
        raise GenuineMixError(str(error)) from error

    ffmpeg_path = Path(ffmpeg_location)
    if ffmpeg_path.is_dir():
        ffmpeg = shutil.which("ffmpeg", path=str(ffmpeg_path))
        ffprobe = shutil.which("ffprobe", path=str(ffmpeg_path))
    else:
        ffmpeg = str(ffmpeg_path)
        search_path = os.pathsep.join(
            filter(None, (str(ffmpeg_path.parent), os.environ.get("PATH", "")))
        )
        ffprobe = shutil.which("ffprobe", path=search_path)
    if not ffmpeg or not ffprobe:
        raise GenuineMixError("FFmpeg and ffprobe are required to create a Genuine Mix.")

    with tempfile.TemporaryDirectory(prefix="fuxion-mix-") as work_dir:
        work_path = Path(work_dir)
        processed_paths = []
        for index, track in enumerate(tracks):
            source_path = work_path / f"source-{index}{track.file_extension}"
            processed_path = work_path / f"track-{index}.flac"
            try:
                with track.audio_file.open("rb") as source, source_path.open(
                    "wb"
                ) as target:
                    shutil.copyfileobj(source, target)
            except OSError as error:
                raise GenuineMixError(
                    f"Could not read the audio file for “{track.title}”."
                ) from error

            if (
                not math.isfinite(track.mix_start_seconds)
                or track.mix_start_seconds < 0
                or (
                    track.mix_end_seconds is not None
                    and (
                        not math.isfinite(track.mix_end_seconds)
                        or track.mix_end_seconds <= track.mix_start_seconds
                    )
                )
            ):
                raise GenuineMixError(f"Trim points for “{track.title}” are invalid.")

            trim_filter = (
                "silenceremove=start_periods=1:start_threshold=-50dB,"
                "areverse,"
                "silenceremove=start_periods=1:start_threshold=-50dB,"
                "areverse,"
                f"atrim=start={track.mix_start_seconds:.6f}"
            )
            if track.mix_end_seconds is not None:
                trim_filter += f":end={track.mix_end_seconds:.6f}"
            trim_filter += ",asetpts=PTS-STARTPTS"
            _run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-af",
                    trim_filter,
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-c:a",
                    "flac",
                    str(processed_path),
                ],
                "Could not remove silence or apply trim points to a track.",
            )
            duration_text = _run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(processed_path),
                ],
                f"Could not read the processed duration of “{track.title}”.",
            )
            try:
                duration = float(duration_text.strip())
            except ValueError as error:
                raise GenuineMixError(
                    f"Could not read the processed duration of “{track.title}”."
                ) from error
            if not math.isfinite(duration) or duration <= 0:
                raise GenuineMixError(
                    f"Trim points leave no audio in “{track.title}”."
                )
            if (
                len(tracks) > 1
                and 0 < playlist.crossfade_seconds
                and duration <= playlist.crossfade_seconds
            ):
                raise GenuineMixError(
                    f"“{track.title}” must be longer than the "
                    f"{playlist.crossfade_seconds:g}-second crossfade."
                )
            processed_paths.append(processed_path)

        filter_complex = _mix_filter(len(processed_paths), playlist.crossfade_seconds)
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for path in processed_paths:
            command.extend(("-i", str(path)))
        command.extend(
            (
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                "-id3v2_version",
                "3",
                str(output_path),
            )
        )
        _run(command, "FFmpeg could not combine the selected tracks.")
        if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
            raise GenuineMixError("FFmpeg did not produce a Genuine Mix MP3.")


def _mix_filter(track_count, crossfade_seconds):
    if track_count == 1:
        return "[0:a]anull[out]"
    if crossfade_seconds == 0:
        inputs = "".join(f"[{index}:a]" for index in range(track_count))
        return f"{inputs}concat=n={track_count}:v=0:a=1[out]"

    filters = []
    current = "[0:a]"
    for index in range(1, track_count):
        output = "[out]" if index == track_count - 1 else f"[mix{index}]"
        filters.append(
            f"{current}[{index}:a]acrossfade=d={crossfade_seconds:g}:"
            f"c1=tri:c2=tri{output}"
        )
        current = output
    return ";".join(filters)


def _run(command, error_message):
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired as error:
        raise GenuineMixError(
            "Genuine Mix generation exceeded the 30-minute limit."
        ) from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip().splitlines()
        if details:
            error_message = f"{error_message} {details[-1][:300]}"
        raise GenuineMixError(error_message) from error
    except OSError as error:
        raise GenuineMixError(f"{error_message} {error}") from error
    return result.stdout


def save_genuine_mix(playlist):
    with tempfile.TemporaryDirectory(prefix="fuxion-mix-output-") as work_dir:
        output_path = Path(work_dir) / "genuine-mix.mp3"
        build_genuine_mix(playlist, output_path)
        old_name = playlist.mix_file.name
        with output_path.open("rb") as output:
            playlist.mix_file.save(
                f"{playlist.slug}-genuine-mix.mp3",
                File(output),
                save=False,
            )
        playlist.save(update_fields=("mix_file",))
        if old_name and old_name != playlist.mix_file.name:
            playlist.mix_file.storage.delete(old_name)
