"""Render the final MP4 from an edit plan, check it, and pull frames for review."""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
from fractions import Fraction
from pathlib import Path

from .edit import FFMPEG, load_run, plan, probe, write_srt

CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1920px;height:1080px;background:#0e0f12;color:#f2f2f2;
  font-family:'Malgun Gothic','Segoe UI',sans-serif;overflow:hidden}
.wrap{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;padding:0 200px}
.kicker{font-size:30px;letter-spacing:.18em;color:#ff3d5a;font-weight:700;margin-bottom:28px}
h1{font-size:132px;margin:0;font-weight:800;letter-spacing:-.02em}
p{font-size:46px;margin:34px 0 0;color:#c9ccd3;line-height:1.45;max-width:1450px}
.line{font-size:36px;color:#9aa0aa;margin-top:18px;font-family:Consolas,'Malgun Gothic',monospace}
.bar{position:absolute;left:200px;bottom:150px;width:160px;height:8px;background:#ff3d5a;border-radius:4px}
</style></head><body><div class="wrap">%s</div><div class="bar"></div></body></html>"""


def render_card(kind: str, spec: dict, out: Path) -> Path:
    from playwright.sync_api import sync_playwright
    parts = []
    if spec.get("kicker"):
        parts.append(f'<div class="kicker">{html.escape(spec["kicker"])}</div>')
    parts.append(f'<h1>{html.escape(spec.get("title", ""))}</h1>')
    if spec.get("subtitle"):
        parts.append(f'<p>{html.escape(spec["subtitle"])}</p>')
    for line in spec.get("lines", []) or []:
        parts.append(f'<div class="line">{html.escape(line)}</div>')
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.set_content(CARD_HTML % "".join(parts))
        pg.wait_for_timeout(150)
        pg.screenshot(path=str(out))
        b.close()
    return out


def _ass_time(sec: float) -> str:
    cs = round(sec * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def write_ass(captions: list[dict], frame: Fraction, sub: dict, w: int, h: int, path: Path) -> None:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{sub.get('font', 'Malgun Gothic')},{sub.get('size', 46)},&H00FFFFFF,&H00FFFFFF,&H64000000,&H64000000,1,0,0,0,100,100,0,0,3,14,0,2,220,220,{sub.get('margin', 54)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [f"Dialogue: 0,{_ass_time(float(c['start'] * frame))},{_ass_time(float(c['end'] * frame))},Default,,0,0,0,,"
             + c["text"].replace("\n", "\\N") for c in captions]
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8-sig")


def _f(v: float) -> str:
    return f"{v:.4f}"


def zoom_exprs(groups: list[dict], w: int, h: int) -> tuple[str, str, str] | None:
    if not groups:
        return None
    z_terms, cx, cy = [], [], []
    for gp in groups:
        a, b, r, s = gp["a"], gp["b"], 0.35, gp["scale"]
        env = f"min(clip((it-{_f(a)})/{r},0,1),clip(({_f(b)}-it)/{r},0,1))"
        z_terms.append(f"{_f(s - 1)}*({env})*({env})*(3-2*({env}))")
        pts = gp["points"]

        def interp(key):
            e = _f(pts[-1][key])
            for k in range(len(pts) - 1, 0, -1):
                t0, t1 = pts[k - 1]["t"], pts[k]["t"]
                v0, v1 = pts[k - 1][key], pts[k][key]
                seg = f"{_f(v0)}+({_f(v1 - v0)})*clip((it-{_f(t0)})/{_f(max(0.05, t1 - t0))},0,1)"
                e = f"if(lt(it,{_f(t1)}),{seg},{e})"
            return e
        cx.append((b, interp("x")))
        cy.append((b, interp("y")))
    z = "1+" + "+".join(z_terms)

    def chain(items, default):
        e = default
        for b, ex in reversed(items):
            e = f"if(lt(it,{_f(b)}),{ex},{e})"
        return e
    x = f"clip(({chain(cx, str(w / 2))})-iw/zoom/2,0,iw-iw/zoom)"
    y = f"clip(({chain(cy, str(h / 2))})-ih/zoom/2,0,ih-ih/zoom)"
    return z, x, y


def render(run_dir: Path, quality: str = "preview", log=print) -> dict:
    t0 = time.monotonic()
    run = load_run(run_dir)
    d = run["dir"]
    info = probe(run["video"])
    pl = plan(run, info)
    fr = info["frame"]
    w, h = info["width"], info["height"]
    fps_s = f"{fr.denominator}/{fr.numerator}"
    out_dir = d / "out"
    out_dir.mkdir(exist_ok=True)
    g = pl["global"]

    inputs = ["-i", str(run["video"])]
    graph = []
    labels = []
    for i, s in enumerate(pl["segments"]):
        graph.append(f"[0:v]trim=start_frame={s['src_in']}:end_frame={s['src_out']},setpts=(PTS-STARTPTS)/{s['speed']}[s{i}]")
        labels.append(f"[s{i}]")
    main = f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps={fps_s},format=yuv420p"
    zx = zoom_exprs(pl["zoom_groups"], w, h)
    if zx:
        z, x, y = zx
        main += f",zoompan=z='{z}':x='{x}':y='{y}':d=1:s={w}x{h}:fps={fps_s}"
    if g["subtitles"].get("burn", True) and pl["captions"]:
        write_ass(pl["captions"], fr, g["subtitles"], w, h, out_dir / "main.ass")
        main += ",ass=out/main.ass"
    main_s = pl["main_seconds"]
    main += f",fade=t=in:st=0:d=0.3,fade=t=out:st={_f(max(0, main_s - 0.3))}:d=0.3,setsar=1[main]"
    graph.append(main)
    order = []
    n = 1
    for kind in ("intro", "outro"):
        spec = pl[kind]
        if not spec:
            continue
        png = render_card(kind, spec, out_dir / f"{kind}.png")
        sec = float(spec["seconds"])
        inputs += ["-loop", "1", "-framerate", fps_s, "-t", _f(sec), "-i", str(png)]
        fade = f"fade=t=out:st={_f(sec - 0.35)}:d=0.35" if kind == "intro" else "fade=t=in:st=0:d=0.35"
        graph.append(f"[{n}:v]scale={w}:{h},fps={fps_s},format=yuv420p,{fade},setsar=1[{kind}]")
        order.append(kind)
        n += 1
    seq = (["intro"] if "intro" in order else []) + ["main"] + (["outro"] if "outro" in order else [])
    graph.append("".join(f"[{k}]" for k in seq) + f"concat=n={len(seq)}:v=1:a=0[out]")
    name = "final.mp4" if quality == "final" else "preview.mp4"
    enc = ["-preset", "slow", "-crf", "16"] if quality == "final" else ["-preset", "veryfast", "-crf", "23"]
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", ";".join(graph),
           "-map", "[out]", "-c:v", "libx264", *enc, "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           f"out/{name}"]
    (out_dir / "ffmpeg_cmd.json").write_text(json.dumps(cmd, ensure_ascii=False, indent=1), encoding="utf-8")
    res = subprocess.run(cmd, cwd=d, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + res.stderr.strip()[-1500:])
    write_srt(pl["captions"], fr, out_dir / "captions.srt", shift=pl["intro_seconds"])
    (out_dir / "plan.json").write_text(json.dumps(pl, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    result = {"video": str(out_dir / name), "srt": str(out_dir / "captions.srt"),
              "seconds": round(probe(out_dir / name)["duration"], 2), "planned_seconds": pl["final_seconds"],
              "render_s": round(time.monotonic() - t0, 1), "zoom_groups": len(pl["zoom_groups"]),
              "captions": len(pl["captions"]), "sync": pl["sync"]}
    log(f"render {quality}: {result['seconds']}s in {result['render_s']}s -> {result['video']}")
    return result


def check(run_dir: Path, video: Path | None = None) -> dict:
    run = load_run(run_dir)
    info = probe(run["video"])
    pl = plan(run, info)
    brief = run["brief"] or {}
    fr = info["frame"]
    errors, warnings, notes = [], [], []

    lo, hi = brief.get("length", [30, 60])
    if not lo <= pl["final_seconds"] <= hi:
        errors.append(f"length {pl['final_seconds']}s is outside {lo}-{hi}s")

    for c in pl["captions"]:
        sec = float((c["end"] - c["start"]) * fr)
        chars = len(c["text"].replace(" ", ""))
        if sec > 0 and chars / sec > 11:
            warnings.append(f"caption '{c['step']}' reads fast: {chars} chars in {sec:.1f}s ({chars / sec:.1f}/s)")
    for a, b in zip(pl["captions"], pl["captions"][1:]):
        if b["start"] < a["end"]:
            warnings.append(f"captions overlap: {a['step']} / {b['step']}")

    s = pl["sync"]
    if s.get("warning"):
        warnings.append(s["warning"])
    else:
        notes.append(f"sync offset {s['offset_s'] * 1000:.0f} ms ({s['offset_frames']} frames), corrected")
        if abs(s.get("offset_frames", 0)) > 30:
            warnings.append(f"capture delay is {s['offset_frames']} frames (corrected, but higher than usual)")
        if s.get("drift_s") is not None and abs(s["drift_s"]) * float(info["fps"]) > 4:
            warnings.append(f"clock drift {s['drift_s'] * 1000:.0f} ms between start and end markers")

    failed = [st["id"] for st in run["log"]["steps"] if not st.get("ok", True)]
    if failed:
        errors.append(f"failed steps: {failed}")
    if run["log"].get("skipped_paid"):
        notes.append(f"paid steps skipped: {run['log']['skipped_paid']}")

    feats = {f["feature"]: f for f in pl["features"]}
    coverage = []
    for level in ("must_show", "nice_to_show"):
        for item in brief.get(level, []) or []:
            f = feats.get(item["id"])
            ok = bool(f and f["seconds"] >= 0.8 and not f.get("failed"))
            coverage.append({"id": item["id"], "level": level, "shown": ok,
                             "at": f["at"] if f else None, "seconds": f["seconds"] if f else 0})
            if not ok:
                (errors if level == "must_show" else notes).append(
                    f"{level} '{item['id']}' is not in the video" + ("" if f else " (no step has this feature)"))
    for r in brief.get("references", []) or []:
        if r.get("use") == "must_look_like":
            notes.append(f"compare by eye with {r['file']}" + (f" ({r['note']})" if r.get("note") else ""))
    if brief.get("avoid"):
        notes.append("check by eye: " + "; ".join(brief["avoid"]))

    out_dir = run["dir"] / "out"
    video = video or next((p for p in (out_dir / "final.mp4", out_dir / "preview.mp4") if p.exists()), None)
    if video:
        res = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video), "-vf",
                              "blackdetect=d=0.5:pix_th=0.05,freezedetect=n=0.002:d=6", "-an", "-f", "null", "-"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
        for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", res):
            warnings.append(f"black frames {m[1]}s-{m[2]}s")
        for m in re.finditer(r"freeze_start: ([\d.]+)", res):
            warnings.append(f"frozen picture for 6s+ from {m[1]}s")
    else:
        notes.append("no render yet: black/frozen frame check skipped")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "notes": notes,
            "final_seconds": pl["final_seconds"], "coverage": coverage, "checked_video": str(video) if video else None}


def frames(run_dir: Path, at: list[float] | None = None, step: str | None = None,
           timeline: str = "output", width: int = 960) -> list[str]:
    run = load_run(run_dir)
    info = probe(run["video"])
    pl = plan(run, info)
    fps = float(info["fps"])
    out_dir = run["dir"] / "out"
    if timeline == "output":
        src = next((p for p in (out_dir / "final.mp4", out_dir / "preview.mp4") if p.exists()), None)
        if src is None:
            raise FileNotFoundError("render first (timeline=output needs out/preview.mp4)")
        shift = pl["intro_seconds"]
        if step:
            a, b = pl["step_out"][step]
            at = [shift + a / fps + 0.15, shift + (a + b) / 2 / fps, shift + b / fps - 0.15]
    else:
        src = run["video"]
        if step:
            st = next(s for s in run["log"]["steps"] if s["id"] == step)
            off = pl["offset_s"]
            at = [st["t_start"] + off + 0.1, (st["t_start"] + st["t_end"]) / 2 + off, st["t_end"] + off - 0.1]
    if not at:
        raise ValueError("give at=[seconds] or step")
    fdir = run["dir"] / "frames"
    fdir.mkdir(exist_ok=True)
    paths = []
    for t in at:
        p = fdir / f"{timeline}_{t:07.2f}.png"
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0, t):.3f}", "-i", str(src),
                        "-frames:v", "1", "-vf", f"scale={width}:-2", str(p)], check=True)
        paths.append(str(p))
    return paths
