"""Shared by the MCP server and the web UI: paths, approval gate, background jobs."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMOS = ROOT / "demos"
RUNS = ROOT / "runs"


DEFAULT_AGENT_MODEL = "composer-2.5"  # Cursor model used unless STEPCUT_AGENT_MODEL or the UI picks another


def agent_model(override: str | None = None) -> str:
    from .envfile import load_env
    return override or load_env(ROOT).get("STEPCUT_AGENT_MODEL") or DEFAULT_AGENT_MODEL


_MODELS: dict = {}


def cursor_models() -> list[dict]:
    """`cursor-agent --list-models`, cached for 10 minutes."""
    import os
    import re
    import shutil
    import subprocess
    if _MODELS.get("at", 0) > time.time() - 600:
        return _MODELS["items"]
    from .envfile import load_env
    env = dict(os.environ)
    key = load_env(ROOT).get("CURSOR_API_KEY")
    if key:
        env["CURSOR_API_KEY"] = key
    exe = shutil.which("cursor-agent") or shutil.which("cursor-agent.cmd")
    out = subprocess.run([exe, "--list-models"], env=env, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=60).stdout if exe else ""
    items = []
    for line in out.splitlines():
        m = re.match(r"^\s*([A-Za-z0-9][\w.\-\[\]=,]*)\s+-\s+(.+?)\s*$", line)
        if m:
            items.append({"id": m[1], "name": m[2].replace("​", "").strip()})
    _MODELS.update(at=time.time(), items=items)
    return items


def slug(name: str) -> str:
    """Folder-safe project name for runs/ (letters, digits, - and _)."""
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "", name) or "project"


def log_err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def project_dir(project: str) -> Path:
    d = DEMOS / project.lower()
    if not d.is_dir() or d.name.startswith("_"):
        known = [p.name for p in DEMOS.iterdir() if p.is_dir() and not p.name.startswith("_")]
        raise FileNotFoundError(f"unknown project '{project}'. Known: {known}")
    return d


def run_dir(run: str) -> Path:
    d = (RUNS / run).resolve()
    if RUNS.resolve() not in d.parents or not (d / "log.json").exists():
        raise FileNotFoundError(f"unknown run '{run}' (use list_runs)")
    return d


def run_id(d: Path) -> str:
    return str(Path(d).resolve().relative_to(RUNS.resolve())).replace("\\", "/")


# ------------------------------------------------------------------ UI mode
UI_FLAG = RUNS / ".ui.json"


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        import os
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def ui_active() -> bool:
    """True while the StepCut web UI is running: recording is then a human button only."""
    try:
        return _pid_alive(int(json.loads(UI_FLAG.read_text(encoding="utf-8"))["pid"]))
    except Exception:
        return False


# ------------------------------------------------------------------ approval
def scenario_hash(d: Path) -> str:
    return hashlib.sha256((d / "scenario.yaml").read_bytes()).hexdigest()


def approval_state(d: Path) -> dict:
    p = d / ".approval.json"
    if not p.exists() or not (d / "scenario.yaml").exists():
        return {"approved": False}
    a = json.loads(p.read_text(encoding="utf-8"))
    same = a.get("sha256") == scenario_hash(d)
    return {"approved": same, "allow_paid": same and a.get("allow_paid", False), "at": a.get("at"),
            "stale": not same}


def approve(d: Path, allow_paid: bool = False, by: str = "cli") -> dict:
    """Human-only: called from the CLI or the UI's Approve button, never exposed as an MCP tool."""
    a = {"sha256": scenario_hash(d), "allow_paid": allow_paid,
         "at": datetime.now().isoformat(timespec="seconds"), "by": by}
    (d / ".approval.json").write_text(json.dumps(a, indent=2), encoding="utf-8")
    return approval_state(d)


# ------------------------------------------------------------------ jobs
JOBS: dict[str, dict] = {}
_record_lock = threading.Lock()


def start_job(kind: str, fn, exclusive: bool = False, **kw) -> dict:
    """Run fn(log=..., **kw) in a thread. exclusive jobs (recordings) never overlap."""
    if exclusive and _record_lock.locked():
        busy = next((j["id"] for j in JOBS.values() if j.get("exclusive") and j["state"] == "running"), "?")
        return {"ok": False, "error": f"another recording is running ({busy})"}
    jid = f"{kind}-{uuid.uuid4().hex[:6]}"
    job = {"id": jid, "kind": kind, "state": "running", "started": time.time(), "log": [],
           "exclusive": exclusive, "args": {k: str(v) for k, v in kw.items()}}
    JOBS[jid] = job

    def log(msg):
        job["log"].append(str(msg))
        log_err(msg)

    def work():
        if exclusive:
            _record_lock.acquire()
        try:
            job["result"] = fn(log=log, **kw)
            job["state"] = "done"
        except Exception as exc:  # noqa: BLE001
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            log_err(traceback.format_exc())
        finally:
            if exclusive:
                _record_lock.release()
            job["seconds"] = round(time.time() - job["started"], 1)

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "job": jid, "state": "running"}


def job_state(jid: str) -> dict:
    j = JOBS.get(jid)
    if not j:
        return {"ok": False, "error": f"unknown job {jid}"}
    out = {k: v for k, v in j.items() if k != "log"}
    out["log_tail"] = j["log"][-40:]
    out["elapsed_s"] = round((j.get("seconds") and j["started"] + j["seconds"] or time.time()) - j["started"], 1)
    return out


def dry_job(d: Path, log, headless: bool = True) -> dict:
    """Test run without OBS. Headless by default so it never takes over the human's screen."""
    from .runner import run
    rd = run(d, recorder="playwright", allow_paid=False, retries=0, log=log, headless=headless)
    take = json.loads((rd / "take1" / "steps.json").read_text(encoding="utf-8"))
    return {"ok": take["ok"], "run": run_id(rd), "error": take.get("error"),
            "steps": [{k: s.get(k) for k in ("id", "ok", "error", "screenshot", "t_start", "t_end")}
                      for s in take["steps"]]}


def record_job(d: Path, allow_paid: bool, log) -> dict:
    from .__main__ import full_run
    s = full_run(d, recorder="obs", allow_paid=allow_paid, log=log)
    if s.get("run_dir"):
        s["run"] = run_id(Path(s["run_dir"]))
    return s
