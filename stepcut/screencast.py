"""Built-in recorder: Chrome's own screencast (CDP Page.startScreencast), no OBS needed.

Chrome sends a JPEG every time the page repaints, stamped with its capture time. A thread on a
separate DevTools websocket receives and acks the frames, so the Playwright driver can keep
sleeping between steps without stalling the stream. On stop, the frames are laid out on the
recording clock (each one lasts until the next) and encoded to a constant 30 fps MP4.

Only the page is captured (the drawn cursor is part of the page), so it also works headless.
"""
from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ScreencastRecorder:
    def __init__(self, port: int, target_id: str, out_dir: Path, quality: int = 92,
                 size: tuple[int, int] = (1920, 1080), fps: int = 30):
        self.port, self.target_id = port, target_id
        self.dir = Path(out_dir)
        self.quality, self.size, self.fps = quality, size, fps
        self.frames: list[tuple[float, str]] = []   # (recording time s, file)
        self.frame_size: tuple[int, int] | None = None
        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self.t0 = self.t0_epoch = 0.0
        self.t_stop = None
        self.error = None

    # ---- clock (same interface as ScreenRecorder / runner.Clock)
    def now(self) -> float:
        return time.monotonic() - self.t0

    def drift(self) -> float:
        return 0.0

    # ---- lifecycle
    def _ws_url(self) -> str:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                    targets = json.loads(r.read())
                for t in targets:
                    if t.get("id") == self.target_id:
                        return t["webSocketDebuggerUrl"]
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("screencast: page target not found on the DevTools port")
            time.sleep(0.2)

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ws = websocket.create_connection(self._ws_url(), timeout=5, suppress_origin=True,
                                               enable_multithread=True)
        self._send(1, "Page.enable")
        self.t0 = time.monotonic()
        self.t0_epoch = time.time()
        self._send(2, "Page.startScreencast", format="jpeg", quality=self.quality,
                   maxWidth=self.size[0], maxHeight=self.size[1], everyNthFrame=1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _send(self, mid: int, method: str, **params) -> None:
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))

    def _loop(self) -> None:
        mid = 100
        self._ws.settimeout(0.5)
        while not self._stop.is_set():
            try:
                msg = json.loads(self._ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    self.error = f"{type(exc).__name__}: {exc}"
                return
            if msg.get("method") != "Page.screencastFrame":
                continue
            p = msg["params"]
            mid += 1
            try:
                self._send(mid, "Page.screencastFrameAck", sessionId=p["sessionId"])
            except Exception:  # noqa: BLE001
                return
            meta = p.get("metadata", {})
            ts = meta.get("timestamp") or time.time()
            t = ts - self.t0_epoch
            f = self.dir / f"{len(self.frames):06d}.jpg"
            f.write_bytes(base64.b64decode(p["data"]))
            if self.frame_size is None and meta.get("deviceWidth"):
                self.frame_size = (meta["deviceWidth"], meta.get("deviceHeight"))  # CSS px
            self.frames.append((t, str(f)))

    def stats(self) -> dict:
        ts = [t for t, _ in self.frames]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        dur = (self.t_stop or self.now())
        return {"frames": len(ts), "avg_fps": round(len(ts) / dur, 1) if dur else 0,
                "max_gap_s": round(max(gaps), 3) if gaps else None,
                "gaps_over_100ms": sum(1 for g in gaps if g > 0.1)}

    def stop(self, out: Path, log=print) -> str | None:
        self.t_stop = self.now()
        try:
            self._send(9, "Page.stopScreencast")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if not self.frames:
            self.error = self.error or "screencast: no frames received"
            return None
        (out.parent / "screencast_frames.json").write_text(
            json.dumps({"t_stop": round(self.t_stop, 4), "t": [round(t, 4) for t, _ in self.frames]}), encoding="utf-8")
        return encode(self.frames, self.t_stop, out, self.size, self.fps, log)


def encode(frames: list[tuple[float, str]], t_end: float, out: Path, size: tuple[int, int], fps: int,
           log=print) -> str:
    """Variable-rate frames -> constant-fps H.264 whose t=0 is the recording clock's 0."""
    import imageio_ffmpeg
    frames = sorted(frames)
    lst = out.parent / "frames.ffconcat"
    lines = ["ffconcat version 1.0"]
    for i, (t, f) in enumerate(frames):
        start = 0.0 if i == 0 else t           # the first frame also covers 0 .. its own time
        nxt = frames[i + 1][0] if i + 1 < len(frames) else t_end
        dur = max(nxt - start, 0.001)
        lines += [f"file '{Path(f).as_posix()}'", f"duration {dur:.4f}"]
    lines.append(f"file '{Path(frames[-1][1]).as_posix()}'")  # concat demuxer needs the last file twice
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    w, h = size
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p")
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(lst), "-vf", vf, "-t", f"{t_end:.3f}",
           "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-movflags", "+faststart", str(out)]
    t0 = time.monotonic()
    subprocess.run(cmd, check=True)
    log(f"  screencast: {len(frames)} frames -> {out.name} ({time.monotonic() - t0:.1f}s)")
    shutil.rmtree(Path(frames[0][1]).parent, ignore_errors=True)
    lst.unlink(missing_ok=True)
    return str(out)
