"""StepCut MCP server (stdio). Any MCP client (Cursor agent, Claude Code) drives recording
and editing through these tools; nothing here clicks the screen or controls an editor.

Human gates that tools cannot bypass:
  * recording needs `python -m stepcut approve demos/<project>` for the exact scenario text
  * paid steps additionally need `approve --allow-paid`
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from mcp.server.mcpserver import Image, MCPServer

from .core import (DEMOS, RUNS, approval_state, dry_job, job_state, log_err, project_dir, record_job,
                   run_dir, start_job, ui_active)

mcp = MCPServer(
    "stepcut",
    instructions=(
        "StepCut records portfolio demo videos of local projects and edits them from a timestamped step log. "
        "Typical flow: get_project -> write_scenario -> dry_run (fix selectors) -> ask the human to approve -> "
        "run_scenario (poll get_job) -> get_run / get_frames -> set_caption / edit_step / set_global -> "
        "render -> check. Captions are polite Korean (~합니다). Never call paid steps without approval."
    ),
)


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _safe(fn):
    """Return errors as data so the agent sees the reason (the SDK would hide it)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001
            _log(traceback.format_exc())
            return _err(f"{type(exc).__name__}: {exc}")
    return wrapper


_log = log_err


_project_dir = project_dir


_run_dir = run_dir




# ------------------------------------------------------------------ projects
@mcp.tool()
@_safe
def list_projects() -> list[dict]:
    """Projects that have a demos/<project>/ folder, with scenario approval state."""
    out = []
    for d in sorted(p for p in DEMOS.iterdir() if p.is_dir() and not p.name.startswith("_")):
        brief = yaml.safe_load((d / "brief.yaml").read_text(encoding="utf-8")) if (d / "brief.yaml").exists() else {}
        out.append({"project": d.name, "title": brief.get("title", d.name), "repo": brief.get("repo"),
                    "has_scenario": (d / "scenario.yaml").exists(), **approval_state(d),
                    "mode": "ui" if ui_active() else "agent"})
    return out


@mcp.tool()
@_safe
def get_project(project: str) -> dict:
    """brief.yaml (what the video must show) and scenario.yaml text for a project."""
    d = _project_dir(project)
    read = lambda n: (d / n).read_text(encoding="utf-8") if (d / n).exists() else None  # noqa: E731
    from .refs import list_refs
    refs = list_refs(d.name)
    return {"brief": read("brief.yaml"), "scenario": read("scenario.yaml"), **approval_state(d),
            "references": refs["references"], "style": refs["style"],
            "hint": "Read every reference with view_reference before drafting; follow demos/_style rules."}


@mcp.tool()
@_safe
def get_references(project: str) -> dict:
    """References attached to a project (refs/ + brief.references) and the shared style texts."""
    from .refs import list_refs
    return list_refs(project)


@mcp.tool()
@_safe
def view_reference(project: str, file: str) -> list[Image | str] | dict:
    """Open one reference. Text returns its content; an image returns the image; a video returns
    ~12 sampled frames with timestamps, its length and scene-cut times (for pacing)."""
    from .refs import view_reference as view
    v = view(project, file)
    if v["kind"] == "image":
        return [Image(path=v["images"][0]), f"image {file}"]
    if v["kind"] == "video":
        head = (f"video {file}: {v['seconds']}s, {v['size'][0]}x{v['size'][1]}, "
                f"scene cuts at {v['scene_cuts'][:40]}; frames at " + ", ".join(f"{f['t']}s" for f in v["frames"]))
        return [head] + [Image(path=f["path"]) for f in v["frames"]]
    return v


@mcp.tool()
@_safe
def add_reference(project: str, path: str, use: str = "info", note: str = "") -> dict:
    """Copy a local file the human pointed to into demos/<project>/refs/ and list it in brief.yaml.
    use: pacing | must_look_like | captions | style | info."""
    from .refs import add_reference as add
    return add(project, src=Path(path), use=use, note=note)


@mcp.tool()
@_safe
def set_feature(project: str, feature_id: str, what: str = "", level: str = "must_show",
                how: str | None = None, remove: bool = False) -> dict:
    """Add, update or remove a feature to show in brief.yaml. level: must_show | nice_to_show."""
    d = _project_dir(project)
    p = d / "brief.yaml"
    brief = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {"project": project}
    for lv in ("must_show", "nice_to_show"):
        brief[lv] = [f for f in brief.get(lv, []) or [] if f.get("id") != feature_id]
    if not remove:
        item = {"id": feature_id, "what": what}
        if how:
            item["how"] = how
        brief.setdefault(level, []).append(item)
    p.write_text(yaml.safe_dump(brief, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "must_show": [f["id"] for f in brief.get("must_show", [])],
            "nice_to_show": [f["id"] for f in brief.get("nice_to_show", [])]}


@mcp.tool()
@_safe
def write_scenario(project: str, scenario_yaml: str) -> dict:
    """Save a new scenario.yaml (validated). This clears the human approval; run dry_run next."""
    d = _project_dir(project)
    try:
        data = yaml.safe_load(scenario_yaml)
        assert isinstance(data, dict) and data.get("steps"), "scenario needs a steps list"
        ids = [s.get("id") for s in data["steps"]]
        assert len(ids) == len(set(ids)), "step ids must be unique"
        for s in data["steps"]:
            assert s.get("do") in {"goto", "click", "hover", "type", "select", "upload", "scroll", "wait",
                                   "pause", "accept_dialogs"}, f"bad action in {s.get('id')}: {s.get('do')}"
    except Exception as exc:
        return _err(f"invalid scenario: {exc}")
    p = d / "scenario.yaml"
    if p.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        (d / "history").mkdir(exist_ok=True)
        (d / "history" / f"scenario-{stamp}.yaml").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    p.write_text(scenario_yaml, encoding="utf-8")
    return {"ok": True, "steps": len(data["steps"]), **approval_state(d),
            "next": "dry_run, then ask the human to run: python -m stepcut approve demos/" + d.name}


# ------------------------------------------------------------------ jobs
@mcp.tool()
@_safe
def get_job(job: str) -> dict:
    """State of a background job (run_scenario / dry_run): running | done | failed, with its log."""
    return job_state(job)


@mcp.tool()
@_safe
def dry_run(project: str) -> dict:
    """Replay the scenario in an invisible (headless) browser without OBS to test selectors.
    Needs no approval and does not touch the human's screen. Returns a job id; poll get_job."""
    return start_job("dry", dry_job, exclusive=False, d=_project_dir(project), headless=True)


@mcp.tool()
@_safe
def run_scenario(project: str, allow_paid: bool = False) -> dict:
    """Record the approved scenario with OBS (retries failed takes), render a preview and check it.
    Requires the human approval of this exact scenario. Returns a job id; poll get_job."""
    if ui_active():
        return _err("In the StepCut UI the human starts recording with the Record button. "
                    "Tell them the plan is ready (dry_run passed) and ask them to press Approve, then Record.")
    d = _project_dir(project)
    st = approval_state(d)
    if not st["approved"]:
        why = "changed since approval" if st.get("stale") else "not approved"
        return _err(f"scenario {why}. Ask the human to run: python -m stepcut approve demos/{d.name}")
    if allow_paid and not st.get("allow_paid"):
        return _err(f"paid steps need: python -m stepcut approve demos/{d.name} --allow-paid")
    return start_job("run", record_job, exclusive=True, d=d, allow_paid=allow_paid)


# ------------------------------------------------------------------ runs
@mcp.tool()
@_safe
def list_runs(project: str | None = None) -> list[dict]:
    """Recorded runs, newest first."""
    out = []
    for log_p in sorted(RUNS.glob("*/*/log.json"), key=lambda p: p.parent.name, reverse=True):
        d = log_p.parent
        if not (d / "events.jsonl").exists():
            continue  # pre-StepCut format
        if project and d.parent.name.lower() != project.lower():
            continue
        s = json.loads((d / "summary.json").read_text(encoding="utf-8")) if (d / "summary.json").exists() else {}
        out.append({"run": f"{d.parent.name}/{d.name}", "recorder": s.get("recorder"), "takes": s.get("takes"),
                    "rendered": (d / "out" / "preview.mp4").exists() or (d / "out" / "final.mp4").exists()})
    return out


@mcp.tool()
@_safe
def get_run(run: str) -> dict:
    """Steps (source times, effective captions, overrides), output timing, feature coverage and sync."""
    from .edit import load_run, plan, probe
    r = load_run(_run_dir(run))
    info = probe(r["video"])
    pl = plan(r, info)
    fps = float(info["fps"])
    over = r["edit"].get("steps") or {}
    caps = {c["step"]: c["text"] for c in pl["captions"]}
    steps = []
    for s in r["log"]["steps"]:
        rng = pl["step_out"].get(s["id"])
        steps.append({"id": s["id"], "do": s["do"], "feature": s.get("feature"), "ok": s.get("ok"),
                      "source": [s["t_start"], s["t_end"]],
                      "output": [round(pl["intro_seconds"] + rng[0] / fps, 2), round(pl["intro_seconds"] + rng[1] / fps, 2)] if rng else None,
                      "caption": caps.get(s["id"], ""), "override": over.get(s["id"])})
    return {"run": run, "steps": steps, "global": r["edit"].get("global"), "features": pl["features"],
            "sync": pl["sync"], "final_seconds": pl["final_seconds"], "intro_seconds": pl["intro_seconds"],
            "zoom_groups": len(pl["zoom_groups"])}


@mcp.tool()
@_safe
def get_events(run: str, step: str | None = None, src: str | None = None, limit: int = 120) -> list[dict]:
    """Event log on the recording clock (src: driver | page | net | step | sync | rec)."""
    from .events import load
    ev = load(_run_dir(run) / "events.jsonl")
    ev = [e for e in ev if (not step or e.get("step") == step) and (not src or e.get("src") == src)]
    return ev[:limit]


@mcp.tool()
@_safe
def get_frames(run: str, step: str | None = None, at: list[float] | None = None,
               timeline: str = "output") -> list[Image | str] | dict:
    """Frames as images. timeline=output uses the latest render (seconds on the final video);
    timeline=source uses the raw recording. With step, returns start / middle / end of that step."""
    from .render import frames
    paths = frames(_run_dir(run), at=at, step=step, timeline=timeline)
    return [Image(path=p) for p in paths] + ["frames: " + ", ".join(paths)]


# ------------------------------------------------------------------ edit
def _edit_change(run: str, tool: str, args: dict, change) -> dict:
    from .edit import load_edit, save_edit
    d = _run_dir(run)
    before = (d / "edit.yaml").read_text(encoding="utf-8") if (d / "edit.yaml").exists() else None
    data = load_edit(d)
    if before is None:
        before = (d / "edit.yaml").read_text(encoding="utf-8")
    msg = change(data)
    save_edit(d, data)
    with (d / "edit_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "tool": tool, "args": args,
                            "before": before}, ensure_ascii=False) + "\n")
    return {"ok": True, "changed": msg, "next": "render(run) to see it"}


def _step_ids(run: str) -> set[str]:
    return {s["id"] for s in json.loads((_run_dir(run) / "log.json").read_text(encoding="utf-8"))["steps"]}


@mcp.tool()
@_safe
def set_caption(run: str, step: str, text: str) -> dict:
    """Replace a step's caption (empty string = no caption). Polite Korean, short enough to read."""
    if step not in _step_ids(run):
        return _err(f"unknown step {step}")

    def ch(data):
        data["steps"].setdefault(step, {})["caption"] = text
        return f"{step}.caption"
    return _edit_change(run, "set_caption", {"step": step, "text": text}, ch)


@mcp.tool()
@_safe
def edit_step(run: str, step: str, drop: bool | None = None, trim_head: float | None = None,
              trim_tail: float | None = None, speed: float | None = None,
              zoom: float | bool | None = None, caption_at: str | None = None,
              reset: bool = False) -> dict:
    """Per-step edit: drop the step, trim seconds from its head/tail, play it at a speed,
    set zoom (false = no zoom, number = zoom scale), caption_at (start = when the step begins,
    action = just before its click; click/select/upload default to action). reset=true clears overrides."""
    if step not in _step_ids(run):
        return _err(f"unknown step {step}")
    vals = {k: v for k, v in {"drop": drop, "trim_head": trim_head, "trim_tail": trim_tail,
                              "speed": speed, "zoom": zoom, "caption_at": caption_at}.items() if v is not None}

    def ch(data):
        if reset:
            data["steps"].pop(step, None)
            return f"{step} reset"
        data["steps"].setdefault(step, {}).update(vals)
        return f"{step}: {vals}"
    return _edit_change(run, "edit_step", {"step": step, **vals, "reset": reset}, ch)


@mcp.tool()
@_safe
def set_global(run: str, key: str, value: Any) -> dict:
    """Set a global edit value by dotted key, e.g. wait=cut, speed=6, zoom.enabled=false,
    zoom.scale=1.3, intro.subtitle='...', outro.lines=['github.com/...'], subtitles.size=42."""
    def ch(data):
        cur = data["global"]
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value
        return f"global.{key} = {value!r}"
    return _edit_change(run, "set_global", {"key": key, "value": value}, ch)


@mcp.tool()
@_safe
def history(run: str) -> list[dict]:
    """Edit history (newest last)."""
    p = _run_dir(run) / "edit_history.jsonl"
    if not p.exists():
        return []
    return [{k: v for k, v in json.loads(line).items() if k != "before"}
            for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


@mcp.tool()
@_safe
def undo(run: str) -> dict:
    """Undo the last edit change."""
    d = _run_dir(run)
    p = d / "edit_history.jsonl"
    lines = [line for line in p.read_text(encoding="utf-8").splitlines() if line.strip()] if p.exists() else []
    if not lines:
        return _err("nothing to undo")
    last = json.loads(lines[-1])
    (d / "edit.yaml").write_text(last["before"], encoding="utf-8")
    p.write_text("".join(line + "\n" for line in lines[:-1]), encoding="utf-8")
    return {"ok": True, "undone": {k: v for k, v in last.items() if k != "before"}}


# ------------------------------------------------------------------ output
@mcp.tool()
@_safe
def render(run: str, quality: str = "preview") -> dict:
    """Render the MP4 (quality: preview | final) with captions, intro/outro cards and click zoom."""
    from .render import render as do_render
    return do_render(_run_dir(run), "final" if quality == "final" else "preview", log=_log)


@mcp.tool()
@_safe
def check(run: str) -> dict:
    """Quality check: length, caption reading speed, sync, failed steps, must_show coverage,
    black / frozen frames in the latest render."""
    from .render import check as do_check
    return do_check(_run_dir(run))


@mcp.tool()
@_safe
def export_resolve(run: str) -> dict:
    """FCPXML + SRT of the main part for manual import into DaVinci Resolve (optional)."""
    from .edit import export_resolve as do_export
    return do_export(_run_dir(run))


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
