"""StepCut web UI: discuss the video plan with the agent, approve, record, review.

  python -m stepcut ui        -> http://127.0.0.1:8790

Listens on 127.0.0.1 only. Buttons only a human may press (approve, record, attach, edit plan)
need a token that is generated per server start and embedded in the page.
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .. import core
from ..chat import chat_for
from ..core import DEMOS, RUNS, approval_state, project_dir, run_dir, run_id, slug, start_job

STATIC = Path(__file__).parent / "static"
TOKEN = secrets.token_urlsafe(24)
app = FastAPI(title="StepCut")


def _human(token: str | None) -> None:
    if token != TOKEN:
        raise HTTPException(403, "this action needs the StepCut page (human)")


def _yaml(p: Path) -> dict:
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


@app.exception_handler(FileNotFoundError)
async def _nf(_req, exc):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)


@app.get("/", response_class=HTMLResponse)
def index():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return html.replace("__TOKEN__", TOKEN)


# ------------------------------------------------------------------ projects
@app.get("/api/projects")
def projects():
    out = []
    for d in sorted(p for p in DEMOS.iterdir() if p.is_dir() and not p.name.startswith("_")):
        b = _yaml(d / "brief.yaml")
        runs = sorted(RUNS.glob(f"{slug(b.get('project', d.name))}/*/events.jsonl"), reverse=True)
        out.append({"project": d.name, "title": b.get("title", d.name), "repo": b.get("repo"),
                    "has_scenario": (d / "scenario.yaml").exists(), **approval_state(d),
                    "runs": len(runs)})
    return {"projects": out, "recording": core._record_lock.locked()}


@app.post("/api/projects")
def new_project(name: str = Body(...), repo: str = Body(""), title: str = Body(""),
                x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    key = "".join(ch for ch in name.lower() if ch.isalnum() or ch in "-_")
    if not key:
        raise HTTPException(400, "name needs letters or digits")
    d = DEMOS / key
    if d.exists():
        raise HTTPException(409, f"{key} exists")
    d.mkdir(parents=True)
    brief = {"project": title or name, "repo": repo, "title": title or name, "tagline": "", "links": [],
             "length": [30, 60], "must_show": [], "nice_to_show": [], "avoid": [".env, API 키, 개인 계정이 보이는 화면"],
             "references": []}
    (d / "brief.yaml").write_text(yaml.safe_dump(brief, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"ok": True, "project": key}


@app.get("/api/p/{project}")
def project(project: str):
    from ..refs import list_refs
    d = project_dir(project)
    brief_t = (d / "brief.yaml").read_text(encoding="utf-8") if (d / "brief.yaml").exists() else ""
    scen_t = (d / "scenario.yaml").read_text(encoding="utf-8") if (d / "scenario.yaml").exists() else ""
    scen = yaml.safe_load(scen_t) if scen_t else {}
    steps = [{"id": s.get("id"), "do": s.get("do"), "caption": s.get("caption", ""), "feature": s.get("feature"),
              "paid": bool(s.get("paid")), "hold": s.get("hold")} for s in (scen or {}).get("steps", [])]
    b = yaml.safe_load(brief_t) if brief_t else {}
    pname = slug((b or {}).get("project", d.name))
    runs = []
    for ev in sorted(RUNS.glob(f"{pname}/*/events.jsonl"), key=lambda p: p.parent.name, reverse=True):
        r = ev.parent
        s = _yaml(r / "summary.json") if (r / "summary.json").exists() else {}
        out = r / "out"
        runs.append({"run": run_id(r), "recorder": s.get("recorder"), "takes": s.get("takes"),
                     "final": (out / "final.mp4").exists(), "preview": (out / "preview.mp4").exists()})
    return {"project": d.name, "brief": brief_t, "scenario": scen_t, "steps": steps, "approval": approval_state(d),
            **list_refs(d.name), "runs": runs, "brief_obj": b}


@app.put("/api/p/{project}/file/{name}")
def save_file(project: str, name: str, text: str = Body(..., embed=True), x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    if name not in ("brief.yaml", "scenario.yaml"):
        raise HTTPException(400, "only brief.yaml / scenario.yaml")
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HTTPException(400, f"YAML error: {exc}") from exc
    (project_dir(project) / name).write_text(text, encoding="utf-8")
    return {"ok": True, "approval": approval_state(project_dir(project))}


# ------------------------------------------------------------------ references
@app.post("/api/p/{project}/refs")
async def upload_ref(project: str, file: UploadFile = File(...), use: str = Form("info"), note: str = Form(""),
                     x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    from ..refs import add_reference
    data = await file.read()
    return add_reference(project, data=data, name=file.filename, use=use, note=note)


@app.delete("/api/p/{project}/refs")
def delete_ref(project: str, file: str, x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    from ..refs import remove_reference
    return remove_reference(project, file)


@app.get("/api/p/{project}/refs/file")
def ref_file(project: str, file: str):
    d = project_dir(project)
    p = (d / file).resolve()
    if d.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ------------------------------------------------------------------ chat
@app.get("/api/p/{project}/chat")
def chat_state(project: str):
    return chat_for(project_dir(project).name).state()


@app.get("/api/models")
def models():
    try:
        items = core.cursor_models()
    except Exception as exc:  # noqa: BLE001
        items, err = [], str(exc)
    else:
        err = None
    return {"default": core.agent_model(), "models": items, "error": err}


@app.post("/api/p/{project}/chat")
def chat_send(project: str, text: str = Body(...), model: str | None = Body(None),
              x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    return chat_for(project_dir(project).name).send(text, model=model or None)


@app.post("/api/p/{project}/chat/stop")
def chat_stop(project: str, x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    chat_for(project_dir(project).name).stop()
    return {"ok": True}


@app.post("/api/p/{project}/chat/reset")
def chat_reset(project: str, x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    chat_for(project_dir(project).name).reset()
    return {"ok": True}


# ------------------------------------------------------------------ actions (human buttons)
@app.post("/api/p/{project}/dry_run")
def dry_run(project: str, visible: bool = Body(False, embed=True), x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    return start_job("dry", core.dry_job, exclusive=visible, d=project_dir(project), headless=not visible)


@app.post("/api/p/{project}/approve")
def approve(project: str, allow_paid: bool = Body(False, embed=True), x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    d = project_dir(project)
    if not (d / "scenario.yaml").exists():
        raise HTTPException(400, "no scenario yet")
    return core.approve(d, allow_paid=allow_paid, by="ui")


@app.post("/api/p/{project}/record")
def record(project: str, allow_paid: bool = Body(False), recorder: str = Body("screen"), hidden: bool = Body(False),
           x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    d = project_dir(project)
    st = approval_state(d)
    if not st["approved"]:
        raise HTTPException(400, "approve the current scenario first")
    if allow_paid and not st.get("allow_paid"):
        raise HTTPException(400, "paid steps were not approved")
    if recorder not in ("screen", "screencast"):
        raise HTTPException(400, "recorder must be screen or screencast")
    return start_job("run", core.record_job, exclusive=True, d=d, allow_paid=allow_paid, recorder=recorder,
                     hidden=hidden)


@app.get("/api/jobs")
def jobs():
    return {"jobs": [core.job_state(j) for j in list(core.JOBS)][-20:]}


@app.get("/api/jobs/{job}")
def job(job: str):
    return core.job_state(job)


# ------------------------------------------------------------------ runs
@app.get("/api/runs/{run:path}/media/{name:path}")
def media(run: str, name: str):
    d = run_dir(run)
    p = (d / name).resolve()
    if d.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/runs/{run:path}/info")
def run_info(run: str):
    from ..mcp_server import check, get_run
    info = get_run(run)
    out = run_dir(run) / "out"
    info["videos"] = {k: f"/api/runs/{run}/media/out/{k}.mp4" for k in ("final", "preview") if (out / f"{k}.mp4").exists()}
    info["check"] = check(run) if info["videos"] else None
    return info


@app.post("/api/runs/{run:path}/render")
def render(run: str, quality: str = Body("preview", embed=True), x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    from ..render import render as do_render
    d = run_dir(run)
    return start_job("render", lambda log: do_render(d, "final" if quality == "final" else "preview", log=log))


@app.post("/api/runs/{run:path}/comment")
def comment(run: str, t: float = Body(...), text: str = Body(...), project: str = Body(...),
            model: str | None = Body(None), x_stepcut_token: str | None = Header(None)):
    _human(x_stepcut_token)
    run_dir(run)
    msg = (f"[영상 코멘트] run `{run}`, 최종 영상 {t:.1f}초 지점: {text}\n"
           f"get_frames(run=\"{run}\", at=[{t:.2f}])로 그 장면을 보고, 고칠 수 있으면 고친 뒤 render와 check까지 해 주세요.")
    return chat_for(project_dir(project).name).send(msg, kind="comment", model=model or None)


# ------------------------------------------------------------------ lifecycle
@app.on_event("startup")
def _startup():
    core.UI_FLAG.parent.mkdir(exist_ok=True)
    core.UI_FLAG.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")


@app.on_event("shutdown")
def _shutdown():
    core.UI_FLAG.unlink(missing_ok=True)


def main(port: int = 8790, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
