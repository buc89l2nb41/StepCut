"""StepCut command line (the MCP server exposes the same functions).

  python -m stepcut check-obs                      OBS connection / versions
  python -m stepcut run demos/spritegen            record (retries failed takes), render preview, check
  python -m stepcut render runs/<project>/<stamp> [--final]
  python -m stepcut check  runs/<project>/<stamp>
  python -m stepcut frames runs/<project>/<stamp> --step open-punch-result [--source]
  python -m stepcut export-resolve runs/<project>/<stamp>
  python -m stepcut approve demos/spritegen [--allow-paid]   human gate before recording
  python -m stepcut mcp                                        MCP server (stdio)
  python -m stepcut agent spritegen                            Cursor agent as director (AGENTS.md)
  python -m stepcut ui                                         web UI: plan with the agent, approve, record
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_check_obs(_args) -> int:
    from .envfile import load_env
    from .obsctl import ObsRecorder
    env = load_env(ROOT)
    rec = ObsRecorder(env.get("OBS_HOST", "localhost"), int(env.get("OBS_PORT", 4455)), env.get("OBS_PASSWORD"))
    try:
        print("OBS:", rec.connect())
        v = rec.client.get_video_settings()
        print("recording now:", rec.client.get_record_status().output_active)
        print("record dir:", rec.client.get_record_directory().record_directory)
        print(f"canvas: {v.base_width}x{v.base_height} @ {v.fps_numerator}/{v.fps_denominator}")
        return 0
    except Exception as exc:
        print("OBS connection failed:", str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
        print("Is OBS open, with Tools > WebSocket Server Settings > Enable checked, and OBS_PASSWORD in .env?")
        return 1


def full_run(scenario: Path, recorder: str = "obs", allow_paid: bool = False, retries: int | None = None,
             render_quality: str = "preview", log=print) -> dict:
    from .render import check, render
    from .runner import run
    t0 = time.monotonic()
    run_dir = run(scenario, recorder=recorder, allow_paid=allow_paid, retries=retries, log=log)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    if not summary["ok"]:
        return summary
    t1 = time.monotonic()
    r = render(run_dir, render_quality, log=log)
    summary.update({"render": r, "render_s": round(time.monotonic() - t1, 1)})
    summary["check"] = check(run_dir)
    summary["total_s"] = round(time.monotonic() - t0, 1)
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def cmd_run(args) -> int:
    s = full_run(Path(args.scenario), args.recorder, args.allow_paid, args.retries,
                 "final" if args.final else "preview")
    _print(s)
    return 0 if s.get("ok") else 1


def cmd_render(args) -> int:
    from .render import render
    _print(render(Path(args.run_dir), "final" if args.final else "preview"))
    return 0


def cmd_check(args) -> int:
    from .render import check
    r = check(Path(args.run_dir))
    _print(r)
    return 0 if r["ok"] else 1


def cmd_frames(args) -> int:
    from .render import frames
    _print(frames(Path(args.run_dir), at=args.at, step=args.step, timeline="source" if args.source else "output"))
    return 0


def cmd_export_resolve(args) -> int:
    from .edit import export_resolve
    _print(export_resolve(Path(args.run_dir)))
    return 0


def cmd_approve(args) -> int:
    from .core import approve
    d = Path(args.project)
    if not (d / "scenario.yaml").exists():
        print(f"no scenario.yaml in {d}")
        return 1
    print((d / "scenario.yaml").read_text(encoding="utf-8"))
    approve(d, allow_paid=args.allow_paid, by="cli")
    print(f"approved {d / 'scenario.yaml'}" + (" (paid steps allowed)" if args.allow_paid else ""))
    print("Any later change to scenario.yaml clears this approval.")
    return 0


def cmd_agent(args) -> int:
    """Run the Cursor agent as the StepCut director (rules in AGENTS.md, tools via .cursor/mcp.json)."""
    import os
    import shutil
    import subprocess
    from .envfile import load_env
    env = dict(os.environ)
    vals = load_env(ROOT)
    if vals.get("CURSOR_API_KEY"):
        env["CURSOR_API_KEY"] = vals["CURSOR_API_KEY"]  # passed through, never printed
    exe = shutil.which("cursor-agent") or shutil.which("cursor-agent.cmd")
    if not exe:
        print("cursor-agent not found on PATH")
        return 1
    task = args.task or (f"{args.project} 프로젝트의 포트폴리오 데모 영상을 AGENTS.md 흐름대로 만들어 줘. "
                         "사람 승인이 필요한 곳에서는 멈추고 무엇을 해야 하는지 알려 줘.")
    cmd = [exe, "-p", "--output-format", "text", "--trust", "--approve-mcps", "--workspace", str(ROOT)]
    from .core import agent_model
    cmd += ["--model", agent_model(args.model)]
    if args.resume:
        cmd += ["--resume", args.resume]
    cmd.append(task)
    return subprocess.call(cmd, env=env, cwd=ROOT)


def cmd_ui(args) -> int:
    from .ui.server import main as serve
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_mcp(_args) -> int:
    from .mcp_server import main as serve
    serve()
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="stepcut")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-obs").set_defaults(fn=cmd_check_obs)
    r = sub.add_parser("run")
    r.add_argument("scenario", help="demos/<project> folder or a scenario.yaml")
    r.add_argument("--recorder", choices=["obs", "playwright"], default="obs")
    r.add_argument("--allow-paid", action="store_true", help="also run steps marked paid: true")
    r.add_argument("--retries", type=int, default=None)
    r.add_argument("--final", action="store_true")
    r.set_defaults(fn=cmd_run)
    for name, fn in (("render", cmd_render), ("check", cmd_check), ("export-resolve", cmd_export_resolve)):
        q = sub.add_parser(name)
        q.add_argument("run_dir")
        if name == "render":
            q.add_argument("--final", action="store_true")
        q.set_defaults(fn=fn)
    f = sub.add_parser("frames")
    f.add_argument("run_dir")
    f.add_argument("--step")
    f.add_argument("--at", type=float, nargs="*")
    f.add_argument("--source", action="store_true", help="frames from the raw recording")
    f.set_defaults(fn=cmd_frames)
    a = sub.add_parser("approve", help="human approval of a scenario before recording")
    a.add_argument("project", help="demos/<project>")
    a.add_argument("--allow-paid", action="store_true")
    a.set_defaults(fn=cmd_approve)
    sub.add_parser("mcp", help="run the MCP server on stdio").set_defaults(fn=cmd_mcp)
    u = sub.add_parser("ui", help="web UI on http://127.0.0.1:8790")
    u.add_argument("--port", type=int, default=8790)
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(fn=cmd_ui)
    g = sub.add_parser("agent", help="run the Cursor agent as director")
    g.add_argument("project")
    g.add_argument("--task", help="override the instruction")
    g.add_argument("--model")
    g.add_argument("--resume", help="resume a previous agent chat id")
    g.set_defaults(fn=cmd_agent)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
