"""Replays a scenario, records it, and writes the timestamped step log."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

from .envfile import load_env
from .events import EventLog
from .web import StepFailed, WebDriver

ROOT = Path(__file__).resolve().parent.parent


def load_scenario(path: Path) -> dict:
    if path.is_dir():
        path = path / "scenario.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data.get("driver", "web") != "web":
        raise SystemExit(f"driver '{data.get('driver')}' is not implemented yet (MVP: web)")
    for i, step in enumerate(data["steps"]):
        step.setdefault("id", f"step{i + 1}")
        step.setdefault("kind", "wait" if step["do"] == "wait" else "show")
    return data


def _url_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status < 500
    except Exception:
        return False


def ensure_app(cfg: dict, log) -> subprocess.Popen | None:
    app = cfg["app"]
    ready = app.get("ready", app["url"])
    if _url_ok(ready):
        return None
    start = app.get("start")
    if not start:
        raise SystemExit(f"{ready} is not responding and the scenario has no app.start")
    log(f"starting app: {start['cmd']} (cwd {start['cwd']})")
    proc = subprocess.Popen(start["cmd"], cwd=start["cwd"], shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + float(start.get("timeout", 90))
    while time.monotonic() < deadline:
        if _url_ok(ready):
            return proc
        time.sleep(1)
    raise SystemExit(f"app did not become ready at {ready}")


class Clock:
    """Fallback clock used when Playwright records the video itself."""

    def __init__(self):
        self.t0 = time.monotonic()

    def now(self) -> float:
        return time.monotonic() - self.t0


def run_take(cfg: dict, take_dir: Path, recorder: str, allow_paid: bool, log) -> dict:
    steps = [s for s in cfg["steps"] if allow_paid or not s.get("paid")]
    skipped = [s["id"] for s in cfg["steps"] if s.get("paid") and not allow_paid]
    obs_rec = None
    video_dir = take_dir / "pw-video" if recorder == "playwright" else None
    driver = WebDriver(cfg, record_video_dir=video_dir)
    result = {"recorder": recorder, "steps": [], "ok": False, "skipped_paid": skipped}
    t_wall = time.monotonic()
    try:
        driver.launch()
        if recorder == "obs":
            from .obsctl import ObsRecorder
            env = load_env(ROOT)
            obs_rec = ObsRecorder(env.get("OBS_HOST", "localhost"), int(env.get("OBS_PORT", 4455)),
                                  env.get("OBS_PASSWORD"), scene=cfg.get("record", {}).get("scene", "StepCut"))
            result["versions"] = obs_rec.connect()
            title = cfg.get("window", {}).get("title") or driver.window_title()
            obs_rec.prepare(title, WebDriver.WINDOW_CLASS, WebDriver.EXE)
            deadline = time.monotonic() + 8
            while obs_rec.capture_size() == (0, 0):
                if time.monotonic() > deadline:
                    raise StepFailed(f"OBS could not find a window titled '{title}'")
                time.sleep(0.2)
            result["capture_size"] = obs_rec.capture_size()
            time.sleep(0.5)
            obs_rec.start()
            clock = obs_rec
        else:
            clock = Clock()
        events = EventLog(clock)
        driver.events = events
        events.emit("rec", "record-start", t=0.0, recorder=recorder)
        lead = float(cfg.get("edit", {}).get("lead_in", 0.6))
        time.sleep(0.15)
        result["sync_start"] = driver.sync_marker("start")
        time.sleep(max(0.0, lead - 0.25))
        for step in steps:
            events.step = step["id"]
            rec = {"id": step["id"], "do": step["do"], "kind": step["kind"],
                   "caption": step.get("caption", ""), "t_start": round(clock.now(), 3)}
            if step.get("feature"):
                rec["feature"] = step["feature"]
            result["steps"].append(rec)
            events.emit("step", "step-start", t=rec["t_start"], do=step["do"], feature=step.get("feature"))
            try:
                driver.do(step)
                rec["t_action_end"] = round(clock.now(), 3)
                driver.check(step.get("expect"), timeout=float(step.get("timeout", 10)))
                rec["t_checked"] = round(clock.now(), 3)
                events.emit("step", "check-ok", t=rec["t_checked"])
                hold = float(step.get("hold", 0))
                if hold:
                    time.sleep(hold)
                rec["t_end"] = round(clock.now(), 3)
                rec["ok"] = True
                driver.drain_page_events()
                events.emit("step", "step-end", t=rec["t_end"])
                log(f"  ok   {step['id']:<22} {rec['t_start']:7.2f}s -> {rec['t_end']:7.2f}s")
            except Exception as exc:
                rec["t_end"] = round(clock.now(), 3)
                rec["ok"] = False
                rec["error"] = str(exc).splitlines()[0][:300]
                driver.drain_page_events()
                events.emit("step", "step-fail", t=rec["t_end"], error=rec["error"])
                log(f"  FAIL {step['id']:<22} {rec['error']}")
                shot = take_dir / f"fail_{step['id']}.png"
                try:
                    driver.page.screenshot(path=str(shot))
                    rec["screenshot"] = str(shot)
                except Exception:
                    pass
                break
        else:
            result["ok"] = True
        events.step = None
        tail = float(cfg.get("edit", {}).get("tail", 1.0))
        time.sleep(max(0.0, tail - 0.4))
        result["sync_end"] = driver.sync_marker("end")
        time.sleep(0.3)
        driver.drain_page_events()
        result["t_last"] = round(clock.now(), 3)
        if obs_rec:
            result["clock_drift_s"] = round(obs_rec.drift(), 4)
            try:
                st = obs_rec.client.get_stats()
                events.emit("rec", "obs-stats", render_skipped=st.render_skipped_frames,
                            output_skipped=st.output_skipped_frames, output_total=st.output_total_frames)
            except Exception:
                pass
        events.emit("rec", "record-stop")
    except Exception as exc:
        result.setdefault("error", str(exc).splitlines()[0][:300])
        log(f"  take error: {result['error']}")
    finally:
        if obs_rec and obs_rec.client:
            try:
                if obs_rec.client.get_record_status().output_active:
                    result["video"] = obs_rec.stop()
            except Exception as exc:
                result.setdefault("error", f"stop failed: {exc}")
            obs_rec.restore()
        if driver.events:
            driver.events.save(take_dir / "events.jsonl")
        pw_video = driver.close()
        if recorder == "playwright" and pw_video:
            result["video"] = pw_video
        result["wall_s"] = round(time.monotonic() - t_wall, 2)
    return result


def run(scenario_path: Path, recorder: str = "obs", allow_paid: bool = False,
        retries: int | None = None, log=print, headless: bool = False) -> Path:
    if scenario_path.is_dir():
        scenario_path = scenario_path / "scenario.yaml"
    cfg = load_scenario(scenario_path)
    if headless:
        if recorder == "obs":
            raise ValueError("OBS recording needs a visible window")
        cfg["_headless"] = True
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    from .core import slug
    run_dir = ROOT / "runs" / slug(cfg["project"]) / stamp
    run_dir.mkdir(parents=True)
    shutil.copy(scenario_path, run_dir / "scenario.yaml")
    if (scenario_path.parent / "brief.yaml").exists():
        shutil.copy(scenario_path.parent / "brief.yaml", run_dir / "brief.yaml")
    t0 = time.monotonic()
    app_proc = ensure_app(cfg, log)
    max_takes = 1 + (cfg.get("retries", 2) if retries is None else retries)
    takes = []
    good = None
    try:
        for n in range(1, max_takes + 1):
            take_dir = run_dir / f"take{n}"
            take_dir.mkdir()
            log(f"take {n}/{max_takes} ({recorder})")
            res = run_take(cfg, take_dir, recorder, allow_paid, log)
            res["take"] = n
            (take_dir / "steps.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
            takes.append(res)
            if res["ok"] and res.get("video"):
                good = res
                break
            # Failed take: keep the file but mark it so it is never edited by mistake.
            if res.get("video") and Path(res["video"]).exists():
                failed = Path(res["video"])
                dest = take_dir / ("FAILED_" + failed.name)
                shutil.move(str(failed), dest)
                res["video"] = str(dest)
                (take_dir / "steps.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if app_proc:
            app_proc.terminate()
    summary = {
        "project": cfg["project"], "scenario": str(scenario_path), "run_dir": str(run_dir),
        "recorder": recorder, "takes": len(takes), "retakes": len(takes) - 1,
        "ok": bool(good), "good_take": good["take"] if good else None,
        "video": good.get("video") if good else None,
        "record_wall_s": round(time.monotonic() - t0, 2),
    }
    if good:
        # Move the recording next to its log so a run folder is self-contained.
        src = Path(good["video"])
        dest = run_dir / f"{slug(cfg['project'])}_{stamp}{src.suffix}"
        if src.exists():
            shutil.move(str(src), dest)
        shutil.copy(run_dir / f"take{good['take']}" / "events.jsonl", run_dir / "events.jsonl")
        summary["video"] = str(dest)
        (run_dir / "log.json").write_text(json.dumps({**summary, "steps": good["steps"],
                                                      "t_last": good.get("t_last"),
                                                      "clock_drift_s": good.get("clock_drift_s"),
                                                      "capture_size": good.get("capture_size"),
                                                      "versions": good.get("versions"),
                                                      "skipped_paid": good.get("skipped_paid"),
                                                      "sync_start": good.get("sync_start"),
                                                      "sync_end": good.get("sync_end")},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "runs" / "LATEST.txt").write_text(str(run_dir), encoding="utf-8")
    return run_dir
