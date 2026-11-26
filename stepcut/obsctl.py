"""OBS control over obs-websocket v5.

Builds an "StepCut" scene holding a single window capture of the demo window, so only
that window is recorded (no notifications, other windows, or the real mouse cursor).
Global audio inputs are muted while recording and restored afterwards.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import obsws_python as obs

WINDOW_INPUT = "StepCut Window"


def _escape(part: str) -> str:
    # OBS stores "title:class:exe" and escapes ':' and '#' inside each part.
    return part.replace("#", "#22").replace(":", "#3A")


@dataclass
class ObsRecorder:
    host: str
    port: int
    password: str | None
    scene: str = "StepCut"
    client: obs.ReqClient | None = None
    _prev_scene: str | None = None
    _muted: dict[str, bool] = field(default_factory=dict)
    _offset: float = 0.0

    def connect(self) -> dict:
        self.client = obs.ReqClient(host=self.host, port=self.port, password=self.password or "", timeout=5)
        ver = self.client.get_version()
        return {"obs": ver.obs_version, "websocket": ver.obs_web_socket_version}

    # ---- scene setup -------------------------------------------------
    def prepare(self, window_title: str, window_class: str, exe: str) -> None:
        c = self.client
        assert c is not None
        scenes = [s["sceneName"] for s in c.get_scene_list().scenes]
        if self.scene not in scenes:
            c.create_scene(self.scene)
        settings = {
            "window": f"{_escape(window_title)}:{_escape(window_class)}:{_escape(exe)}",
            "priority": 1,          # match title first
            "method": 2,            # Windows Graphics Capture (works for Chromium)
            "cursor": False,        # the page draws its own cursor
            "client_area": True,
            "capture_audio": False,
        }
        inputs = [i["inputName"] for i in c.get_input_list().inputs]
        if WINDOW_INPUT in inputs:
            c.set_input_settings(WINDOW_INPUT, settings, True)
        else:
            c.create_input(self.scene, WINDOW_INPUT, "window_capture", settings, True)
        items = c.get_scene_item_list(self.scene).scene_items
        item_id = next((i["sceneItemId"] for i in items if i["sourceName"] == WINDOW_INPUT), None)
        if item_id is None:
            item_id = c.create_scene_item(self.scene, WINDOW_INPUT, True).scene_item_id
        video = c.get_video_settings()
        c.set_scene_item_transform(self.scene, item_id, {
            "positionX": 0, "positionY": 0, "alignment": 5,
            "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsAlignment": 0,
            "boundsWidth": float(video.base_width), "boundsHeight": float(video.base_height),
        })
        self._prev_scene = c.get_current_program_scene().current_program_scene_name
        c.set_current_program_scene(self.scene)
        self._mute_globals()

    def _mute_globals(self) -> None:
        c = self.client
        special = c.get_special_inputs()
        for attr in ("desktop1", "desktop2", "mic1", "mic2", "mic3", "mic4"):
            name = getattr(special, attr, None)
            if not name:
                continue
            try:
                self._muted[name] = c.get_input_mute(name).input_muted
                c.set_input_mute(name, True)
            except Exception:
                pass

    def capture_size(self) -> tuple[int, int]:
        """Size of the captured window (0,0 while OBS has not found it yet)."""
        c = self.client
        items = c.get_scene_item_list(self.scene).scene_items
        item_id = next(i["sceneItemId"] for i in items if i["sourceName"] == WINDOW_INPUT)
        t = c.get_scene_item_transform(self.scene, item_id).scene_item_transform
        return int(t.get("sourceWidth", 0)), int(t.get("sourceHeight", 0))

    # ---- recording ---------------------------------------------------
    def start(self) -> None:
        c = self.client
        if c.get_record_status().output_active:
            raise RuntimeError("OBS is already recording; stop it first.")
        c.start_record()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            st = c.get_record_status()
            if st.output_active and st.output_duration > 0:
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("OBS did not start recording")
        # Calibrate a monotonic offset so step timestamps cost no round trip.
        best = None
        for _ in range(5):
            t0 = time.monotonic()
            dur = c.get_record_status().output_duration / 1000.0
            t1 = time.monotonic()
            cand = (t1 - t0, (t0 + t1) / 2 - dur)
            best = cand if best is None or cand[0] < best[0] else best
        self._offset = best[1]

    def now(self) -> float:
        return time.monotonic() - self._offset

    def drift(self) -> float:
        """Local clock minus OBS clock, in seconds (checks the calibration)."""
        dur = self.client.get_record_status().output_duration / 1000.0
        return self.now() - dur

    def stop(self) -> str:
        resp = self.client.stop_record()
        path = getattr(resp, "output_path", None)
        # Wait until the muxer has finished the file.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.client.get_record_status().output_active:
            time.sleep(0.1)
        return path

    def restore(self) -> None:
        c = self.client
        if c is None:
            return
        for name, was in self._muted.items():
            try:
                c.set_input_mute(name, was)
            except Exception:
                pass
        self._muted.clear()
        if self._prev_scene:
            try:
                c.set_current_program_scene(self._prev_scene)
            except Exception:
                pass
