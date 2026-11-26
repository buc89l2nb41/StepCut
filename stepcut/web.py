"""Playwright driver for web projects.

Elements are found by role / label / text / CSS (never screen coordinates). Because the
OBS capture hides the real mouse, a drawn cursor is moved to each element before it is
clicked so the viewer can follow the action.
"""
from __future__ import annotations

import math
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page, sync_playwright

from .events import MARKER_JS, PAGE_EVENTS_JS

CURSOR_JS = r"""
(() => {
  if (window.__scCursor) return;
  const make = () => {
    if (document.getElementById('__sc_cursor')) return;
    const c = document.createElement('div');
    c.id = '__sc_cursor';
    c.innerHTML = '<svg width="28" height="28" viewBox="0 0 28 28"><path d="M4 2 L4 22 L9.5 17 L13 25 L16.5 23.5 L13 15.5 L20 15.5 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(c.style, {position:'fixed', left:'0', top:'0', width:'28px', height:'28px',
      zIndex:'2147483647', pointerEvents:'none', transform:'translate(-200px,-200px)',
      transition:'transform 0ms linear', filter:'drop-shadow(0 1px 2px rgba(0,0,0,.5))'});
    document.documentElement.appendChild(c);
  };
  window.__scCursor = {
    x: -200, y: -200,
    move(x, y, ms) { make(); const c = document.getElementById('__sc_cursor');
      c.style.transition = `transform ${ms}ms cubic-bezier(.4,0,.2,1)`;
      c.style.transform = `translate(${x - 4}px, ${y - 2}px)`; this.x = x; this.y = y; },
    pulse() { const r = document.createElement('div');
      Object.assign(r.style, {position:'fixed', left:(this.x-18)+'px', top:(this.y-18)+'px', width:'36px', height:'36px',
        borderRadius:'50%', border:'3px solid #ffd54a', zIndex:'2147483646', pointerEvents:'none',
        transition:'transform 380ms ease-out, opacity 380ms ease-out', transform:'scale(.4)', opacity:'1'});
      document.documentElement.appendChild(r); requestAnimationFrame(() => { r.style.transform='scale(1.3)'; r.style.opacity='0'; });
      setTimeout(() => r.remove(), 450); }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
})();
"""


class StepFailed(Exception):
    pass


def locate(page: Page, target: Any) -> Locator:
    if isinstance(target, str):
        loc = page.locator(target)
    elif isinstance(target, dict):
        exact = target.get("exact", False)
        if "role" in target:
            name = target.get("name")
            if isinstance(name, str) and name.startswith("/") and name.endswith("/"):
                name = re.compile(name[1:-1])
            loc = page.get_by_role(target["role"], name=name, exact=exact)
        elif "label" in target:
            loc = page.get_by_label(target["label"], exact=exact)
        elif "text" in target:
            loc = page.get_by_text(target["text"], exact=exact)
        elif "placeholder" in target:
            loc = page.get_by_placeholder(target["placeholder"], exact=exact)
        elif "alt" in target:
            loc = page.get_by_alt_text(target["alt"], exact=exact)
        elif "css" in target:
            loc = page.locator(target["css"])
        else:
            raise ValueError(f"unknown target: {target}")
        if "has_text" in target:
            loc = loc.filter(has_text=target["has_text"])
    else:
        raise ValueError(f"unknown target: {target!r}")
    nth = target.get("nth") if isinstance(target, dict) else None
    return loc.nth(nth) if nth is not None else loc.first


class WebDriver:
    WINDOW_CLASS = "Chrome_WidgetWin_1"
    EXE = "chrome.exe"

    def __init__(self, cfg: dict, record_video_dir: Path | None = None):
        self.cfg = cfg
        self.window = cfg.get("window", {})
        self.record_video_dir = record_video_dir
        self._pw = None
        self.ctx = None
        self.page: Page | None = None
        self._profile = None
        self.events = None   # EventLog, attached once the recording clock runs
        self.dpr = 1.0

    # ---- lifecycle ---------------------------------------------------
    def launch(self) -> None:
        self._pw = sync_playwright().start()
        self._profile = tempfile.mkdtemp(prefix="stepcut-profile-")
        url = self.cfg["app"]["url"]
        scale = float(self.window.get("scale", 1.0))
        args = [f"--app={url}", f"--force-device-scale-factor={scale}", "--disable-infobars",
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"]
        if self.window.get("fullscreen", True):
            args.append("--start-fullscreen")
        else:
            w, h = self.window.get("width", 1600), self.window.get("height", 900)
            args += [f"--window-size={w},{h}", "--window-position=40,40"]
        kwargs: dict[str, Any] = dict(headless=False, args=args, no_viewport=True,
                                      ignore_default_args=["--enable-automation"],
                                      locale="ko-KR")
        headless = bool(self.cfg.get("_headless"))
        if headless:
            # Test runs: same 1920x1080 picture, but no window on the human's screen.
            kwargs = dict(headless=True, locale="ko-KR", device_scale_factor=scale,
                          viewport={"width": round(1920 / scale), "height": round(1080 / scale)},
                          args=["--disable-features=Translate", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        if self.record_video_dir:
            kwargs["record_video_dir"] = str(self.record_video_dir)
            kwargs["record_video_size"] = {"width": 1920, "height": 1080}
        self.ctx = self._pw.chromium.launch_persistent_context(self._profile, **kwargs)
        self.ctx.add_init_script(CURSOR_JS)
        self.ctx.add_init_script(PAGE_EVENTS_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        if headless:
            self.page.goto(url, wait_until="domcontentloaded")
        self.page.set_default_timeout(15000)
        self.page.wait_for_load_state("domcontentloaded")
        self.page.evaluate(CURSOR_JS)
        self.page.evaluate(PAGE_EVENTS_JS)
        self.dpr = float(self.page.evaluate("() => devicePixelRatio"))
        self.page.on("requestfinished", self._on_request)

    def _on_request(self, req) -> None:
        if not self.events or req.resource_type not in ("fetch", "xhr", "document"):
            return
        try:
            tm = req.timing
            resp = req.response()
        except Exception:
            return
        start = tm.get("startTime", 0)
        if start <= 0:
            return
        end = tm.get("responseEnd", -1)
        url = req.url.split("?")[0]
        self.events.emit_epoch("net", "request", start, method=req.method, url=url,
                               status=resp.status if resp else None,
                               ms=round(end, 1) if end and end > 0 else None)

    def drain_page_events(self) -> None:
        if not self.events:
            return
        try:
            items = self.page.evaluate("() => (window.__scEvents || []).splice(0)")
        except Exception:
            return
        for it in items:
            epoch = it.pop("epoch")
            typ = it.pop("type")
            if "x" in it:
                it["px"] = [round(it["x"] * self.dpr), round(it["y"] * self.dpr)]
            self.events.emit_epoch("page", typ, epoch, **it)

    def sync_marker(self, label: str, frames: int = 4) -> float:
        """Shows the magenta corner block; returns its paint time on the recording clock."""
        epoch = self.page.evaluate(MARKER_JS, frames)
        ev = self.events.emit_epoch("sync", "marker", epoch, label=label, frames=frames)
        return ev["t"]

    def window_title(self) -> str:
        return self.page.title()

    def close(self) -> str | None:
        video_path = None
        try:
            if self.page and self.page.video:
                video_path = self.page.video.path()
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            if self._profile:
                shutil.rmtree(self._profile, ignore_errors=True)
        return str(video_path) if video_path else None

    # ---- cursor ------------------------------------------------------
    def _move_to(self, loc: Locator) -> None:
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        if not box:
            raise StepFailed("element has no box (not visible)")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        pos = self.page.evaluate("() => window.__scCursor ? [window.__scCursor.x, window.__scCursor.y] : [-200,-200]")
        dist = math.hypot(x - pos[0], y - pos[1]) if pos[0] > -100 else 600
        ms = int(min(900, max(280, dist * 0.7)))
        if self.events:
            self.events.emit("driver", "cursor-move", px=[round(x * self.dpr), round(y * self.dpr)], ms=ms)
        self.page.evaluate("([x,y,ms]) => window.__scCursor && window.__scCursor.move(x,y,ms)", [x, y, ms])
        self.page.mouse.move(x, y, steps=8)  # real hover state for the app
        time.sleep(ms / 1000 + 0.08)
        self._last_box = [round(v * self.dpr) for v in (box["x"], box["y"], box["width"], box["height"])]
        self._last_px = [round(x * self.dpr), round(y * self.dpr)]

    def _emit_target(self, kind: str, step: dict, **data) -> None:
        if self.events:
            self.events.emit("driver", kind, target=str(step.get("target")),
                             px=getattr(self, "_last_px", None), box=getattr(self, "_last_box", None), **data)

    def _pulse(self) -> None:
        self.page.evaluate("() => window.__scCursor && window.__scCursor.pulse()")

    # ---- actions -----------------------------------------------------
    def do(self, step: dict) -> None:
        action = step["do"]
        page = self.page
        self._last_px = self._last_box = None
        if action == "goto":
            page.goto(step.get("url", self.cfg["app"]["url"]), wait_until="networkidle")
            page.evaluate(CURSOR_JS)
            page.evaluate("() => window.__scCursor.move(innerWidth*0.55, innerHeight*0.55, 0)")
        elif action == "click":
            loc = locate(page, step["target"])
            loc.wait_for(state="visible")
            self._move_to(loc)
            self._pulse()
            self._emit_target("click", step)
            loc.click()
        elif action == "hover":
            loc = locate(page, step["target"])
            loc.wait_for(state="visible")
            self._move_to(loc)
        elif action == "type":
            loc = locate(page, step["target"])
            loc.wait_for(state="visible")
            self._move_to(loc)
            self._pulse()
            loc.click()
            if step.get("clear", True):
                loc.fill("")
            self._emit_target("type", step, text=step["text"])
            loc.press_sequentially(step["text"], delay=int(step.get("delay_ms", 70)))
            if self.events:
                self.events.emit("driver", "type-end")
        elif action == "select":
            loc = locate(page, step["target"])
            self._move_to(loc)
            loc.select_option(step["value"])
        elif action == "upload":
            # File dialogs are OS windows; set the file directly and show the cursor on the control.
            loc = locate(page, step["target"])
            try:
                self._move_to(loc)
                self._pulse()
            except StepFailed:
                pass  # hidden <input type=file> is fine
            self._emit_target("upload", step, file=Path(step["file"]).name)
            loc.set_input_files(step["file"])
        elif action == "scroll":
            if self.events:
                self.events.emit("driver", "scroll")
            if "target" in step:
                loc = locate(page, step["target"])
                loc.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
            else:
                page.evaluate("(dy) => window.scrollBy({top: dy, behavior: 'smooth'})", step.get("by", 400))
            time.sleep(0.6)
        elif action == "wait":
            self.check(step.get("until", {}), timeout=float(step.get("timeout", 60)))
        elif action == "pause":
            pass
        elif action == "accept_dialogs":
            page.once("dialog", lambda d: d.accept())
        else:
            raise ValueError(f"unknown action: {action}")

    # ---- checks ------------------------------------------------------
    def check(self, expect: dict | None, timeout: float = 10.0) -> None:
        if not expect:
            return
        page = self.page
        ms = int(timeout * 1000)
        try:
            if "visible" in expect:
                locate(page, expect["visible"]).wait_for(state="visible", timeout=ms)
            if "hidden" in expect:
                locate(page, expect["hidden"]).wait_for(state="hidden", timeout=ms)
            if "text" in expect:
                page.get_by_text(expect["text"]).first.wait_for(state="visible", timeout=ms)
            if "js" in expect:
                page.wait_for_function(expect["js"], timeout=ms)
            if "value" in expect:
                spec = expect["value"]
                loc = locate(page, spec["target"])
                deadline = time.monotonic() + timeout
                while loc.input_value() != spec["equals"]:
                    if time.monotonic() > deadline:
                        raise StepFailed(f"value is {loc.input_value()!r}, expected {spec['equals']!r}")
                    time.sleep(0.1)
            if "images_loaded" in expect:
                sel = expect["images_loaded"]
                page.wait_for_function(
                    "(sel) => { const imgs=[...document.querySelectorAll(sel)]; return imgs.length>0 && imgs.every(i=>i.complete && i.naturalWidth>0); }",
                    arg=sel, timeout=ms)
        except StepFailed:
            raise
        except Exception as exc:  # Playwright TimeoutError etc.
            raise StepFailed(f"check failed {expect}: {str(exc).splitlines()[0]}") from exc
