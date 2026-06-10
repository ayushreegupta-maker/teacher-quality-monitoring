"""ffmpeg / ffprobe helpers for working with long videos."""

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def check_ffmpeg_available() -> None:
    """Raise RuntimeError with a helpful message if ffmpeg/ffprobe aren't on PATH."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            f"Required tools missing on PATH: {', '.join(missing)}. "
            "Install via `brew install ffmpeg` on macOS."
        )


def probe_duration_seconds(video_path: Path) -> float:
    """Use ffprobe to get the total duration of a video in seconds."""
    check_ffmpeg_available()
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def extract_segment(
    input_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> None:
    """Extract a segment from a video using ffmpeg.

    Video stream is copied (fast, no re-encode). Audio stream is re-encoded
    to AAC because some sources use codecs (e.g. pcm_mulaw) that are not
    storable in an MP4 container with stream-copy.

    Uses `-ss` before `-i` for fast keyframe seek. Accuracy is ±1 sec relative
    to the requested start, which is fine for 5-min windows.
    """
    check_ffmpeg_available()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-ss", f"{start_seconds:.2f}",
        "-i", str(input_path),
        "-t", f"{duration_seconds:.2f}",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "128k",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        str(output_path),
    ]
    log.info(
        f"extracting segment: start={start_seconds:.0f}s "
        f"duration={duration_seconds:.0f}s → {output_path.name}"
    )
    subprocess.run(cmd, check=True)


def hms_to_seconds(hms: str) -> int:
    """Parse an HH:MM:SS (or MM:SS) string into integer seconds."""
    parts = hms.strip().split(":")
    try:
        if len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(float(parts[2]))
        elif len(parts) == 2:
            h, m, s = 0, int(parts[0]), int(float(parts[1]))
        else:
            raise ValueError
    except ValueError:
        raise ValueError(f"Unparseable timestamp: {hms!r}")
    return h * 3600 + m * 60 + s


def seconds_to_hms(seconds: float) -> str:
    """Format integer seconds as HH:MM:SS."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
