"""Reference material for a project video: demos/<project>/refs/ + `references:` in brief.yaml,
and shared style in demos/_style/ (read for every project)."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .core import DEMOS, project_dir
from .edit import FFMPEG, probe

TEXT = {".md", ".txt", ".yaml", ".yml", ".json", ".srt"}
IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO = {".mp4", ".webm", ".mov", ".mkv"}
USES = ("pacing", "must_look_like", "captions", "style", "info")


def _brief(d: Path) -> tuple[Path, dict]:
    p = d / "brief.yaml"
    return p, (yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}) or {}


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    return "text" if ext in TEXT else "image" if ext in IMAGE else "video" if ext in VIDEO else "other"


def style_texts() -> list[dict]:
    sd = DEMOS / "_style"
    out = []
    if sd.is_dir():
        for p in sorted(sd.iterdir()):
            if p.is_file() and p.suffix.lower() in TEXT:
                out.append({"file": f"_style/{p.name}", "text": p.read_text(encoding="utf-8")[:6000]})
    return out


def list_refs(project: str) -> dict:
    d = project_dir(project)
    _, brief = _brief(d)
    listed = {r.get("file"): r for r in brief.get("references", []) or []}
    items = []
    rdir = d / "refs"
    if rdir.is_dir():
        for p in sorted(rdir.iterdir()):
            if p.is_file():
                rel = f"refs/{p.name}"
                r = listed.pop(rel, {})
                items.append({"file": rel, "kind": kind_of(p), "use": r.get("use", "info"),
                              "note": r.get("note", ""), "bytes": p.stat().st_size})
    for rel, r in listed.items():
        items.append({"file": rel, "missing": True, **r})
    return {"references": items, "style": style_texts()}


def add_reference(project: str, src: Path | None = None, data: bytes | None = None, name: str | None = None,
                  use: str = "info", note: str = "") -> dict:
    if use not in USES:
        raise ValueError(f"use must be one of {USES}")
    d = project_dir(project)
    rdir = d / "refs"
    rdir.mkdir(exist_ok=True)
    fname = re.sub(r"[^\w.\-가-힣 ]", "_", name or (src.name if src else "ref.bin")).strip() or "ref.bin"
    dest = rdir / fname
    if src is not None:
        src = Path(src)
        if not src.is_file():
            raise FileNotFoundError(str(src))
        if src.name.lower().startswith(".env"):
            raise PermissionError("refusing to copy .env files")
        shutil.copy(src, dest)
    elif data is not None:
        dest.write_bytes(data)
    else:
        raise ValueError("give src or data")
    p, brief = _brief(d)
    refs = [r for r in brief.get("references", []) or [] if r.get("file") != f"refs/{fname}"]
    refs.append({"file": f"refs/{fname}", "use": use, **({"note": note} if note else {})})
    brief["references"] = refs
    p.write_text(yaml.safe_dump(brief, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "file": f"refs/{fname}", "kind": kind_of(dest), "use": use}


def remove_reference(project: str, file: str) -> dict:
    d = project_dir(project)
    target = (d / file).resolve()
    if d.resolve() / "refs" not in target.parents:
        raise PermissionError("only files under refs/ can be removed")
    if target.exists():
        target.unlink()
    p, brief = _brief(d)
    brief["references"] = [r for r in brief.get("references", []) or [] if r.get("file") != file]
    p.write_text(yaml.safe_dump(brief, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "removed": file}


def scene_cuts(video: Path, threshold: float = 0.3) -> list[float]:
    res = subprocess.run([FFMPEG, "-hide_banner", "-i", str(video), "-vf",
                          f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    return [round(float(m), 2) for m in re.findall(r"pts_time:([\d.]+)", res)]


def view_reference(project: str, file: str, max_frames: int = 12) -> dict:
    """Text -> content. Image -> its path. Video -> sampled frames + length + scene cuts."""
    d = project_dir(project)
    p = (d / file).resolve()
    if not p.is_file() or d.resolve() not in p.parents:
        raise FileNotFoundError(file)
    k = kind_of(p)
    if k == "text":
        return {"kind": k, "text": p.read_text(encoding="utf-8", errors="replace")[:12000]}
    if k == "image":
        return {"kind": k, "images": [str(p)]}
    if k != "video":
        return {"kind": k, "note": "unsupported file type; describe it in the note"}
    info = probe(p)
    dur = info["duration"] or 1
    n = max(1, min(max_frames, int(dur // 3) or 1))
    out_dir = d / "refs" / ".frames" / p.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        f = out_dir / f"{t:07.2f}.jpg"
        if not f.exists():
            subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(p),
                            "-frames:v", "1", "-vf", "scale=640:-2", str(f)], check=True)
        frames.append({"t": round(t, 2), "path": str(f)})
    return {"kind": k, "seconds": round(dur, 2), "size": [info["width"], info["height"]],
            "scene_cuts": scene_cuts(p), "frames": frames}
