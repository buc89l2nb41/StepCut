"""Built-in screen recorder: ffmpeg Desktop Duplication (ddagrab), no OBS needed.

Records the whole monitor at a steady frame rate (the app runs full screen on it), with the
system cursor hidden because StepCut draws its own. The window is visible on screen
while recording, and the magenta sync markers correct the small start delay afterwards.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path


class ScreenRecorder:
    def __init__(self, out: Path, fps: int = 30, monitor: int = 0, crf: int = 16):
        self.out = Path(out)
        self.fps, self.monitor, self.crf = fps, monitor, crf
        self.proc: subprocess.Popen | None = None
        self.t0 = 0.0
        self.error = None
        self._err: list[str] = []

    def now(self) -> float:
        return time.monotonic() - self.t0

    def drift(self) -> float:
        return 0.0

    def start(self) -> None:
        import imageio_ffmpeg
        src = (f"ddagrab=output_idx={self.monitor}:framerate={self.fps}:draw_mouse=0,"
               f"hwdownload,format=bgra")
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "info", "-stats", "-stats_period", "0.05",
               "-f", "lavfi", "-i", src, "-vf", "format=yuv420p",
               "-fps_mode", "cfr", "-r", str(self.fps),  # ddagrab skips ticks when nothing changes: fill them
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(self.crf), "-movflags", "+faststart",
               str(self.out)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)
        threading.Thread(target=self._read_err, daemon=True).start()
        # The clock starts when ffmpeg reports its first encoded frame (markers fix the rest).
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("screen recorder failed: " + " | ".join(self._err[-3:]))
            if any(re.search(r"frame=\s*[1-9]", x) for x in self._err[-5:]):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("screen recorder produced no frames")
        self.t0 = time.monotonic()

    def _read_err(self) -> None:
        buf = b""
        while True:
            ch = self.proc.stderr.read(1)
            if not ch:
                break
            if ch in b"\r\n":
                if buf:
                    self._err.append(buf.decode("utf-8", "replace"))
                    del self._err[:-50]
                buf = b""
            else:
                buf += ch

    def stats(self) -> dict:
        last = next((x for x in reversed(self._err) if "frame=" in x), "")
        m = re.search(r"frame=\s*(\d+)", last)
        d = re.search(r"dup=(\d+)", last)
        dr = re.search(r"drop=(\d+)", last)
        return {"frames": int(m[1]) if m else None, "dup": int(d[1]) if d else 0, "drop": int(dr[1]) if dr else 0,
                "fps": self.fps}

    def stop(self, out: Path | None = None, log=print) -> str | None:
        if not self.proc:
            return None
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.error = "screen recorder did not stop in time"
        if self.proc.returncode not in (0, None) and not self.out.exists():
            self.error = "screen recorder failed: " + " | ".join(self._err[-3:])
            return None
        log(f"  screen: {self.stats()} -> {self.out.name}")
        return str(self.out)
