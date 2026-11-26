"""Planning chat with the Cursor agent, one conversation per project.

Each turn runs `cursor-agent -p --output-format stream-json --resume <chat id>` so the
conversation carries over. History is kept in demos/<project>/chat/messages.jsonl.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from .core import ROOT, agent_model, log_err, project_dir
from .envfile import load_env

PREAMBLE = (
    "[StepCut UI] 프로젝트: {project}. 지금은 사람과 영상 계획을 의논하는 단계입니다. "
    "AGENTS.md 규칙을 따르되, 녹화는 사람이 UI의 승인·녹화 버튼으로 시작하므로 run_scenario는 부르지 않습니다. "
    "사람이 하지 말라고 한 것은 하지 않습니다. 생각 중간 메모를 포함해 모든 글은 한국어로 씁니다. "
    "dry_run은 보이지 않는 브라우저에서 돌아 화면을 가리지 않으므로 선택자 확인에 써도 됩니다. "
    "먼저 get_project로 brief, 시나리오, 참고 자료(references, style)를 보고, 참고 자료는 view_reference로 열어 보세요. "
    "계획을 바꾸면 set_feature / write_scenario로 저장하고 dry_run으로 확인한 뒤 결과를 짧게 알려 주세요. "
    "녹화 뒤의 수정 요청에는 get_frames로 그 장면을 보고 set_caption / edit_step / set_global → render → check 순서로 고칩니다. "
    "답은 한국어로, 짧게, 무엇을 바꿨는지와 사람이 정할 것을 분명히 적어 주세요.\n\n"
)


def _env() -> dict:
    env = dict(os.environ)
    vals = load_env(ROOT)
    if vals.get("CURSOR_API_KEY"):
        env["CURSOR_API_KEY"] = vals["CURSOR_API_KEY"]
    return env


def _exe() -> str:
    exe = shutil.which("cursor-agent") or shutil.which("cursor-agent.cmd")
    if not exe:
        raise FileNotFoundError("cursor-agent is not on PATH")
    return exe


def _tool_name(tc: dict) -> str:
    for key, val in tc.items():
        if key.endswith("ToolCall") and isinstance(val, dict):
            args = val.get("args", {}) or {}
            if key == "mcpToolCall":
                return args.get("toolName") or args.get("name", "mcp")
            if key == "getMcpToolsToolCall":
                return f"(lookup {args.get('toolName', '')})"
            return key.replace("ToolCall", "")
    return "tool"


class Chat:
    def __init__(self, project: str):
        self.project = project
        self.dir = project_dir(project) / "chat"
        self.dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.live: dict | None = None
        self.proc: subprocess.Popen | None = None

    # ---- storage
    @property
    def chat_id(self) -> str | None:
        p = self.dir / "chat_id"
        return p.read_text(encoding="utf-8").strip() if p.exists() else None

    def _new_chat(self) -> str:
        out = subprocess.run([_exe(), "create-chat"], env=_env(), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60).stdout.strip()
        cid = out.splitlines()[-1].strip() if out else ""
        if len(cid) < 8:
            raise RuntimeError("cursor-agent create-chat returned no id (is CURSOR_API_KEY set?)")
        (self.dir / "chat_id").write_text(cid, encoding="utf-8")
        return cid

    def messages(self) -> list[dict]:
        p = self.dir / "messages.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _append(self, msg: dict) -> None:
        with (self.dir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def reset(self) -> None:
        if self.live and self.live.get("running"):
            raise RuntimeError("a turn is running")
        for n in ("chat_id", "messages.jsonl"):
            (self.dir / n).unlink(missing_ok=True)

    # ---- turns
    def send(self, text: str, kind: str = "user", model: str | None = None) -> dict:
        with self.lock:
            if self.live and self.live.get("running"):
                return {"ok": False, "error": "agent is still answering"}
            first = self.chat_id is None
            use = agent_model(model)
            self._append({"role": "user", "kind": kind, "text": text, "model": use,
                          "at": datetime.now().isoformat(timespec="seconds")})
            self.live = {"running": True, "text": "", "tools": [], "started": time.time(), "error": None,
                         "model_id": use}
            threading.Thread(target=self._turn, args=(text, first, use), daemon=True).start()
            return {"ok": True}

    def _turn(self, text: str, first: bool, model: str) -> None:
        live = self.live
        try:
            cid = self.chat_id or self._new_chat()
            prompt = (PREAMBLE.format(project=self.project) if first else
                      f"[StepCut UI · {self.project}] ") + text
            cmd = [_exe(), "-p", "--output-format", "stream-json", "--stream-partial-output", "--trust",
                   "--approve-mcps", "--workspace", str(ROOT), "--resume", cid, "--model", model]
            cmd.append(prompt)
            self.proc = subprocess.Popen(cmd, env=_env(), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace")
            final = None
            for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "system" and ev.get("subtype") == "init":
                    live["model"] = ev.get("model")
                elif t == "assistant" and ev.get("timestamp_ms") is not None:
                    piece = "".join(c.get("text", "") for c in ev.get("message", {}).get("content", [])
                                    if c.get("type") == "text")
                    seg = live.setdefault("_seg", "")
                    if piece and seg and piece == seg:
                        continue  # full message repeating the deltas just streamed
                    live["text"] += piece
                    live["_seg"] = seg + piece
                elif t == "tool_call":
                    name = _tool_name(ev.get("tool_call", {}))
                    if name.startswith("(lookup"):
                        continue
                    live["_seg"] = ""
                    if ev.get("subtype") == "started":
                        live["tools"].append({"id": ev.get("call_id"), "name": name, "state": "running"})
                    else:
                        for tl in live["tools"]:
                            if tl["id"] == ev.get("call_id"):
                                tl["state"] = "done"
                elif t == "result":
                    final = ev.get("result")
                    if ev.get("is_error"):
                        live["error"] = str(final)[:500]
            self.proc.wait()
            if self.proc.returncode and not final:
                err = (self.proc.stderr.read() or "").strip()[-600:]
                live["error"] = live["error"] or f"cursor-agent exited {self.proc.returncode}: {err}"
            if final:
                live["text"] = final
        except Exception as exc:  # noqa: BLE001
            live["error"] = f"{type(exc).__name__}: {exc}"
            log_err(live["error"])
        finally:
            live["running"] = False
            live["seconds"] = round(time.time() - live["started"], 1)
            self._append({"role": "agent", "text": live["text"] or "", "tools": [x["name"] for x in live["tools"]],
                          "error": live["error"], "model": live.get("model"), "seconds": live["seconds"],
                          "at": datetime.now().isoformat(timespec="seconds")})

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def state(self) -> dict:
        return {"messages": self.messages(), "live": self.live, "chat_id": self.chat_id}


_CHATS: dict[str, Chat] = {}


def chat_for(project: str) -> Chat:
    key = project.lower()
    if key not in _CHATS:
        _CHATS[key] = Chat(key)
    return _CHATS[key]
