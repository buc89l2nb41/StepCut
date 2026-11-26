"""Edit model: log.json + events.jsonl + edit.yaml -> an edit plan (segments, captions,
zoom windows, cards), plus SRT / FCPXML exports.

log.json and events.jsonl are never changed. All edit decisions live in edit.yaml:

  global: {wait, speed, keep, auto_wait, min_wait, min_caption, zoom: {...}, intro: {...},
           outro: {...}, subtitles: {...}}
  steps:  {<step id>: {caption, caption_at, drop, trim_head, trim_tail, speed, zoom}}
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

import imageio_ffmpeg
import yaml

from . import events as evlib
from .sync import measure

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

DEFAULT_GLOBAL = {
    "wait": "speed", "speed": 8, "keep": 0.4, "auto_wait": 2.0, "min_wait": 1.2, "min_caption": 1.8,
    "lead": 0.4, "tail": 0.5,
    "zoom": {"enabled": True, "scale": 1.35, "on": ["click", "type", "upload"],
             "before": 0.55, "after": 0.7, "ramp": 0.35},
    "subtitles": {"burn": True, "font": "Malgun Gothic", "size": 46, "margin": 54},
    "intro": {"enabled": True, "seconds": 2.6},
    "outro": {"enabled": True, "seconds": 2.6},
}


# ---------------------------------------------------------------- files
def probe(video: Path) -> dict:
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    v = re.search(r"Video: .*?, (\d{3,5})x(\d{3,5}).*?, ([\d.]+) (?:fps|tbr)", out)
    w, h, fps = (int(v[1]), int(v[2]), float(v[3])) if v else (1920, 1080, 60.0)
    known = {23.98: Fraction(1001, 24000), 29.97: Fraction(1001, 30000), 59.94: Fraction(1001, 60000)}
    frame = next((fd for k, fd in known.items() if abs(fps - k) < 0.01), Fraction(1, round(fps)))
    return {"duration": dur, "width": w, "height": h, "frame": frame, "fps": 1 / frame}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def default_edit(run_dir: Path) -> dict:
    """Initial edit.yaml: scenario `edit:` defaults + titles from brief.yaml."""
    cfg = yaml.safe_load((run_dir / "scenario.yaml").read_text(encoding="utf-8"))
    g = {k: v for k, v in (cfg.get("edit") or {}).items() if k not in ("lead_in",)}
    brief_p = run_dir / "brief.yaml"
    brief = yaml.safe_load(brief_p.read_text(encoding="utf-8")) if brief_p.exists() else {}
    title = brief.get("title") or cfg["project"]
    tagline = brief.get("tagline", "")
    g.setdefault("intro", {}).update({k: v for k, v in {"title": title, "subtitle": tagline}.items()
                                      if k not in g["intro"]})
    g.setdefault("outro", {}).update({k: v for k, v in {"title": title, "lines": brief.get("links", [])}.items()
                                      if k not in g["outro"]})
    return {"global": g, "steps": {}}


def load_edit(run_dir: Path) -> dict:
    p = run_dir / "edit.yaml"
    if not p.exists():
        save_edit(run_dir, default_edit(run_dir))
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("global", {})
    data.setdefault("steps", {})
    return data


def save_edit(run_dir: Path, data: dict) -> None:
    (run_dir / "edit.yaml").write_text(
        "# StepCut edit decisions. log.json / events.jsonl stay untouched; render rebuilds from these.\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_run(run_dir: Path) -> dict:
    run_dir = Path(run_dir).resolve()
    log = json.loads((run_dir / "log.json").read_text(encoding="utf-8"))
    brief_p = run_dir / "brief.yaml"
    return {
        "dir": run_dir, "log": log,
        "events": evlib.load(run_dir / "events.jsonl"),
        "edit": load_edit(run_dir),
        "brief": yaml.safe_load(brief_p.read_text(encoding="utf-8")) if brief_p.exists() else {},
        "video": Path(log["video"]),
    }


def get_sync(run: dict, info: dict) -> dict:
    p = run["dir"] / "sync.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    s = measure(run["video"], run["log"], float(info["fps"]))
    p.write_text(json.dumps(s, indent=2), encoding="utf-8")
    return s


# ---------------------------------------------------------------- plan
def plan(run: dict, info: dict) -> dict:
    log, ed = run["log"], run["edit"]
    g = _deep_merge(DEFAULT_GLOBAL, ed.get("global"))
    over = ed.get("steps") or {}
    sync = get_sync(run, info)
    off = float(sync.get("offset_s") or 0.0)
    ss, se = sync.get("sync_start") or {}, sync.get("sync_end") or {}
    if ss.get("video") is not None and se.get("video") is not None and se["logged"] > ss["logged"]:
        o1, o2 = ss["video"] - ss["logged"], se["video"] - se["logged"]

        def at(t: float) -> float:  # log clock -> video clock, linear between the two markers
            return t + o1 + (o2 - o1) * (t - ss["logged"]) / (se["logged"] - ss["logged"])
    else:
        def at(t: float) -> float:
            return t + off
    fps = float(info["fps"])
    steps = [dict(s, t_start=at(s["t_start"]), t_end=at(s["t_end"]),
                  t_action_end=at(s.get("t_action_end", s["t_end"])),
                  t_checked=at(s.get("t_checked", s["t_end"]))) for s in log["steps"]]
    dur = info["duration"] or 1e9

    lo = steps[0]["t_start"] - float(g["lead"])
    if log.get("sync_start") is not None:
        lo = max(lo, at(log["sync_start"]) + 0.15)
    hi = steps[-1]["t_end"] + float(g["tail"])
    if log.get("sync_end") is not None:
        hi = min(hi, at(log["sync_end"]) - 0.05)
    lo, hi = max(0.0, lo), min(hi, dur)

    raw: list[dict] = []

    def add(a, b, sp, sid):
        if b - a > 1e-3:
            raw.append({"a": a, "b": b, "speed": sp, "step": sid})

    for i, s in enumerate(steps):
        o = over.get(s["id"]) or {}
        a = lo if i == 0 else s["t_start"]
        b = steps[i + 1]["t_start"] if i + 1 < len(steps) else hi
        if o.get("drop"):
            continue
        a += float(o.get("trim_head", 0))
        b -= float(o.get("trim_tail", 0))
        if b <= a:
            continue
        if o.get("speed"):
            add(a, b, float(o["speed"]), s["id"])
            continue
        act, chk = s["t_action_end"], s["t_checked"]
        waits = []
        if s["kind"] == "wait":
            waits.append((max(a, s["t_start"]), min(b, chk)))
        elif chk - act > float(g["auto_wait"]):
            waits.append((max(a, act), min(b, chk)))
        cur = a
        for wa, wb in waits:
            if wb <= wa:
                continue
            add(cur, wa, 1.0, s["id"])
            if wb - wa < float(g["min_wait"]):
                add(wa, wb, 1.0, s["id"])
            elif g["wait"] == "cut" and wb - wa > 2 * float(g["keep"]) + 0.2:
                add(wa, wa + float(g["keep"]), 1.0, s["id"])
                add(wb - float(g["keep"]), wb, 1.0, s["id"])
            elif g["wait"] == "cut":
                add(wa, wb, 1.0, s["id"])
            else:
                add(wa, wb, float(g["speed"]), s["id"])
            cur = wb
        add(cur, b, 1.0, s["id"])

    segs, out = [], 0
    step_out: dict[str, list[int]] = {}
    for r in raw:
        fi, fo = round(r["a"] * fps), round(r["b"] * fps)
        if fo <= fi:
            continue
        if segs and segs[-1]["speed"] == r["speed"] and segs[-1]["src_out"] == fi:
            segs[-1]["src_out"] = fo
            segs[-1]["out_dur"] = max(1, round((fo - segs[-1]["src_in"]) / r["speed"]))
        else:
            segs.append({"src_in": fi, "src_out": fo, "speed": r["speed"],
                         "out_in": out, "out_dur": max(1, round((fo - fi) / r["speed"])),
                         "kind": "wait" if r["speed"] != 1.0 else "show"})
        out = segs[-1]["out_in"] + segs[-1]["out_dur"]
        dur_f = max(1, round((fo - fi) / r["speed"]))
        rng = step_out.setdefault(r["step"], [out - dur_f, out])
        rng[1] = out
    total = out

    def to_out(t_src: float) -> float | None:
        f = t_src * fps
        for s in segs:
            if s["src_in"] <= f < s["src_out"]:
                return float((s["out_in"] + (f - s["src_in"]) / s["speed"]) / fps)
        return None

    # captions + markers (main-relative frames)
    captions, markers = [], []
    min_cap = round(float(g["min_caption"]) * fps)
    # Steps whose result appears on click: the caption starts just before the click, not while
    # the cursor is still travelling there (default per action, override with caption_at).
    caption_at_default = {"click": "action", "select": "action", "upload": "action"}
    cap_start: dict[str, int] = {}
    for s in steps:
        if s["id"] not in step_out:
            continue
        a, b = step_out[s["id"]]
        mode = (over.get(s["id"]) or {}).get("caption_at") or caption_at_default.get(s["do"], "start")
        if mode == "action":
            t = to_out(s["t_action_end"] - float(g.get("caption_lead", 0.25)))
            if t is not None:
                a = min(max(a, round(t * fps)), b - 1)
        cap_start[s["id"]] = a
    starts = sorted(cap_start.values())
    for s in steps:
        if s["id"] not in step_out:
            continue
        a, b = cap_start[s["id"]], step_out[s["id"]][1]
        text = (over.get(s["id"]) or {}).get("caption", s.get("caption", ""))
        markers.append({"frame": step_out[s["id"]][0], "name": s["id"], "note": text,
                        "color": "Blue" if s.get("ok", True) else "Red"})
        if text:
            nxt = min([x for x in starts if x > a] + [total])
            captions.append({"start": a, "end": max(b, min(a + min_cap, nxt)), "text": text, "step": s["id"]})
    # keep the previous caption up while the cursor travels to the next click (no blank gap)
    hold_gap = round(float(g.get("caption_hold_gap", 3.0)) * fps)
    for c, n in zip(captions, captions[1:]):
        if 0 < n["start"] - c["end"] <= hold_gap:
            c["end"] = n["start"]

    # zoom windows on clicks (output seconds)
    zooms = []
    zc = g["zoom"]
    cap = log.get("capture_size") or [info["width"], info["height"]]
    sx, sy = info["width"] / max(1, cap[0]), info["height"] / max(1, cap[1])
    if zc.get("enabled", True):
        for ev in run["events"]:
            if ev.get("src") != "driver" or ev.get("type") not in zc["on"] or not ev.get("px"):
                continue
            o = over.get(ev.get("step"), {}) or {}
            if o.get("zoom") is False:
                continue
            t = to_out(at(ev["t"]))
            if t is None:
                continue
            scale = float(o["zoom"]) if isinstance(o.get("zoom"), (int, float)) and o["zoom"] is not True else float(zc["scale"])
            zooms.append({"t": round(t, 3), "x": ev["px"][0] * sx, "y": ev["px"][1] * sy, "scale": scale,
                          "step": ev.get("step"), "a": max(0.0, t - zc["before"]), "b": t + zc["after"]})
    # merge overlapping windows into groups (pan between points inside a group)
    groups: list[dict] = []
    for z in zooms:
        if groups and z["a"] <= groups[-1]["b"]:
            gp = groups[-1]
            gp["b"] = max(gp["b"], z["b"])
            gp["points"].append(z)
            gp["scale"] = max(gp["scale"], z["scale"])
        else:
            groups.append({"a": z["a"], "b": z["b"], "scale": z["scale"], "points": [z]})
    for gp in groups:
        gp["b"] = min(gp["b"], total / fps)

    # feature coverage (output seconds)
    feats: dict[str, dict] = {}
    for s in steps:
        f = s.get("feature")
        if not f:
            continue
        rng = step_out.get(s["id"])
        shown = (rng[1] - rng[0]) / fps if rng else 0.0
        cur = feats.setdefault(f, {"feature": f, "seconds": 0.0, "at": None, "steps": []})
        cur["seconds"] = round(cur["seconds"] + shown, 2)
        cur["steps"].append(s["id"])
        if rng and cur["at"] is None and shown > 0:
            cur["at"] = round(rng[0] / fps, 2)
        if not s.get("ok", True):
            cur["failed"] = True

    intro = g["intro"] if g["intro"].get("enabled", True) else None
    outro = g["outro"] if g["outro"].get("enabled", True) else None
    intro_s = float(intro["seconds"]) if intro else 0.0
    return {
        "global": g, "sync": sync, "offset_s": off,
        "segments": segs, "markers": markers, "captions": captions, "zoom_groups": groups,
        "features": list(feats.values()), "step_out": step_out,
        "total_frames": total, "main_seconds": round(total / fps, 3),
        "intro": intro, "outro": outro, "intro_seconds": intro_s,
        "final_seconds": round(intro_s + total / fps + (float(outro["seconds"]) if outro else 0.0), 2),
    }


# ---------------------------------------------------------------- exports
def _srt_time(sec: float) -> str:
    ms = round(sec * 1000)
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(captions: list[dict], frame: Fraction, path: Path, shift: float = 0.0) -> None:
    lines = []
    for i, c in enumerate(captions, 1):
        a, b = float(c["start"] * frame) + shift, float(c["end"] * frame) + shift
        lines += [str(i), f"{_srt_time(a)} --> {_srt_time(b)}", c["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8-sig")


def _t(frames: int, frame: Fraction) -> str:
    v = frames * frame
    return f"{v.numerator}/{v.denominator}s" if v.denominator != 1 else f"{v.numerator}s"


def write_fcpxml(pl: dict, info: dict, video: Path, name: str, path: Path) -> None:
    """Main part only (cuts, speed-ups, markers). Zoom and cards are in the MP4 render."""
    fr = info["frame"]
    asset_frames = round(info["duration"] * info["fps"])
    clips = []
    for i, s in enumerate(pl["segments"]):
        inner = []
        for m in pl["markers"]:
            if s["out_in"] <= m["frame"] < s["out_in"] + s["out_dur"]:
                src = s["src_in"] + round((m["frame"] - s["out_in"]) * s["speed"])
                inner.append(f'        <marker start="{_t(src, fr)}" duration="{_t(1, fr)}" '
                             f'value={quoteattr(m["name"])} note={quoteattr(m["note"])}/>')
        if s["speed"] != 1.0:
            inner.insert(0, "        <timeMap>\n"
                         f'          <timept time="0s" value="{_t(s["src_in"], fr)}" interp="linear"/>\n'
                         f'          <timept time="{_t(s["out_dur"], fr)}" value="{_t(s["src_out"], fr)}" interp="linear"/>\n'
                         "        </timeMap>")
        clip_name = quoteattr("%02d_%s" % (i + 1, s["kind"]))
        clips.append(f'      <asset-clip ref="r2" name={clip_name} offset="{_t(s["out_in"], fr)}" '
                     f'start="{_t(s["src_in"], fr)}" duration="{_t(s["out_dur"], fr)}" tcFormat="NDF">\n'
                     + "\n".join(inner) + ("\n" if inner else "") + "      </asset-clip>")
    fd = f"{fr.numerator}/{fr.denominator}s"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="r1" frameDuration="{fd}" width="{info['width']}" height="{info['height']}"/>
    <asset id="r2" name={quoteattr(video.stem)} start="0s" duration="{_t(asset_frames, fr)}" hasVideo="1" hasAudio="0" format="r1">
      <media-rep kind="original-media" src={quoteattr(Path(video).resolve().as_uri())}/>
    </asset>
  </resources>
  <library>
    <event name="StepCut">
      <project name={quoteattr(name)}>
        <sequence format="r1" duration="{_t(pl['total_frames'], fr)}" tcStart="0s" tcFormat="NDF">
          <spine>
{chr(10).join(clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""
    path.write_text(xml, encoding="utf-8")


def export_resolve(run_dir: Path) -> dict:
    run = load_run(run_dir)
    info = probe(run["video"])
    pl = plan(run, info)
    name = f"{run['log']['project']} {run['dir'].name}"
    fcpxml, srt = run["dir"] / "timeline.fcpxml", run["dir"] / "timeline_captions.srt"
    write_fcpxml(pl, info, run["video"], name, fcpxml)
    write_srt(pl["captions"], info["frame"], srt)
    return {"fcpxml": str(fcpxml), "srt": str(srt),
            "note": "Resolve: File > Import > Timeline (FCPXML), then drag the SRT onto the timeline. "
                    "Zoom and intro/outro cards exist only in the MP4 render."}
