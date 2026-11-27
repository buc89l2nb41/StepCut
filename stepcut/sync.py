"""Finds the magenta sync markers in the video and measures the log -> video offset."""
from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _is_marker(r: int, g: int, b: int) -> bool:
    return r > 190 and b > 190 and g < 90


def find_marker(video: Path, around: float, fps: float, window: float = 0.8) -> float | None:
    """First frame time (s) in [around-window, around+window] whose top-left corner is magenta."""
    start = max(0.0, around - window)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{2 * window:.3f}",
           "-i", str(video), "-vf", "crop=12:12:8:8,scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    for i in range(0, len(raw) - 2, 3):
        if _is_marker(raw[i], raw[i + 1], raw[i + 2]):
            return start + (i // 3) / fps
    return None


def measure(video: Path, log: dict, fps: float) -> dict:
    out: dict = {"fps": fps}
    offsets = []
    for key in ("sync_start", "sync_end"):
        t = log.get(key)
        if t is None:
            continue
        # the end marker is looked for where the start marker says the video has shifted to
        shift = offsets[0] if offsets else 0.0
        seen = find_marker(video, t + shift, fps)
        out[key] = {"logged": t, "video": None if seen is None else round(seen, 4)}
        if seen is not None:
            offsets.append(seen - t)
    if offsets:
        out["offset_s"] = round(sum(offsets) / len(offsets), 4)
        out["drift_s"] = round(offsets[-1] - offsets[0], 4) if len(offsets) == 2 else None
        out["offset_frames"] = round(out["offset_s"] * fps, 2)
    else:
        out["offset_s"] = 0.0
        out["warning"] = "sync markers not found in video; using the log clock as is"
    return out
