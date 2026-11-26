"""Event log on the recording clock.

Every event gets `t` = seconds since the first frame of the recording. Page-side events
carry an epoch time (performance.timeOrigin + performance.now()), converted here with the
offset between the wall clock and the recording clock.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class EventLog:
    def __init__(self, clock):
        self.clock = clock
        self.items: list[dict] = []
        self.step: str | None = None

    def rec_from_epoch(self, epoch_s: float) -> float:
        # wall clock and recording clock move together; the offset is measured now
        return epoch_s - (time.time() - self.clock.now())

    def emit(self, src: str, type_: str, t: float | None = None, **data) -> dict:
        ev = {"t": round(self.clock.now() if t is None else t, 4), "src": src, "type": type_}
        if self.step:
            ev["step"] = self.step
        ev.update({k: v for k, v in data.items() if v is not None})
        self.items.append(ev)
        return ev

    def emit_epoch(self, src: str, type_: str, epoch_ms: float, **data) -> dict:
        return self.emit(src, type_, t=self.rec_from_epoch(epoch_ms / 1000.0), **data)

    def save(self, path: Path) -> None:
        self.items.sort(key=lambda e: e["t"])
        with path.open("w", encoding="utf-8") as f:
            for ev in self.items:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# Injected into the page: records what the app actually received, plus image loads.
PAGE_EVENTS_JS = r"""
(() => {
  if (window.__scEvents) return;
  window.__scEvents = [];
  const now = () => performance.timeOrigin + performance.now();
  const desc = (el) => {
    if (!el || !el.tagName) return '';
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.alt || '').trim().slice(0, 40);
    return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (name ? ' "' + name + '"' : '');
  };
  const push = (type, e, extra) => window.__scEvents.push(Object.assign({type, epoch: now(), target: desc(e && e.target)}, extra || {}));
  addEventListener('click', e => push('click', e, {x: e.clientX, y: e.clientY}), true);
  addEventListener('input', e => push('input', e, {len: (e.target && e.target.value || '').length}), true);
  addEventListener('change', e => push('change', e), true);
  addEventListener('keydown', e => push('keydown', e, {key: e.key.length === 1 ? e.key : e.key}), true);
  addEventListener('load', e => { if (e.target && e.target.tagName === 'IMG') push('img-load', e, {src: (e.target.currentSrc || '').slice(-80)}); }, true);
  addEventListener('loadeddata', e => push('video-ready', e), true);
})();
"""

# Sync marker: a magenta corner block for a few frames; reports the paint time.
MARKER_JS = r"""
(frames) => new Promise(resolve => {
  const m = document.createElement('div');
  Object.assign(m.style, {position:'fixed', left:'0', top:'0', width:'40px', height:'40px',
    background:'#ff00ff', zIndex:'2147483647', pointerEvents:'none'});
  document.documentElement.appendChild(m);
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const shown = performance.timeOrigin + performance.now();
    let n = 0;
    const tick = () => { if (++n >= frames) { m.remove(); resolve(shown); } else requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }));
})
"""
