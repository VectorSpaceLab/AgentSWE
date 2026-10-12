#!/usr/bin/env python3
"""Evaluator owned, rendered GUI control service.

The Candidate receives only the loopback URL of this process.  This service owns
the Playwright browser and is deliberately a small allow listed RPC surface:
there is no page evaluation, CDP endpoint, arbitrary navigation, fixture HTTP
forwarder, filesystem access, or state mutation operation.  Every observation
and action is recorded in evaluator only storage so the bridge evidence can be
bound to actual browser input.

The module is executable because the harness starts it in a separate process.
It is also intentionally dependency optional: a missing Playwright/browser is
reported as infrastructure failure by the parent harness instead of producing
an untrusted score.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.server
import json
import os
import secrets
import signal
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PROTOCOL = "evaluator-controlled-browser-v1"
MAX_BODY = 64 * 1024
MAX_TEXT = 16_384
MAX_WAIT = 10.0
ALLOWED_KEYS = {
    "Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp",
    "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp",
    "PageDown", "Control+A", "Control+C", "Control+V", "Shift+Tab", "Control+Home", "Control+End",
}
ALLOWED_TAGS = {"button", "input", "select", "textarea", "a", "option", "canvas", "img"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


class BrowserController:
    """Own one fresh browser context and expose only rendered interactions."""

    def __init__(self, fixture_url: str, viewport: dict[str, int], evidence: Path, token: str, browser_session: str):
        parsed = urlsplit(fixture_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("fixture origin must be loopback HTTP")
        width, height = int(viewport["width"]), int(viewport["height"])
        if not (100 <= width <= 5000 and 100 <= height <= 5000):
            raise ValueError("viewport outside bounded range")
        self.fixture_url = fixture_url.rstrip("/") + "/"
        self.viewport = {"width": width, "height": height}
        self.evidence = evidence
        self.token = token
        self.browser_session = browser_session
        self.lock = threading.RLock()
        self.sequence = 0
        self.handle_map: dict[str, tuple[Any, dict[str, Any]]] = {}
        self.receipts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.started = now()
        self.closed = False
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self.initial_observation = None
        self.short_tmp = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("Playwright is unavailable: " + str(exc)) from exc
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "screenshots").mkdir(exist_ok=True)
        # Chromium's Unix profile sockets have a 108-byte path limit.
        self.short_tmp = tempfile.mkdtemp(prefix='gui-', dir='/tmp')
        os.environ['TMPDIR'] = self.short_tmp
        tempfile.tempdir = self.short_tmp
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(
                headless=True, executable_path=os.environ.get('CHROMIUM') or None,
                args=['--no-sandbox'] if os.geteuid() == 0 else [],
            )
            self._context = self._browser.new_context(
                viewport=self.viewport,
                device_scale_factor=1,
                java_script_enabled=True,
                service_workers="block",
                accept_downloads=False,
            )
            # The fixture accepts state recording only with this evaluator-owned
            # session cookie.  It is never placed in Candidate environment.
            self._context.add_cookies([{
                "name": "evaluator_browser_session", "value": self.browser_session,
                "url": self.fixture_url, "httpOnly": True, "sameSite": "Strict",
            }])
            self.page = self._context.new_page()
            # Keep the browser on the supplied origin.  Navigations initiated by
            # the fixture are checked by the request handler below as well.
            self._context.route('**/*', self._request_guard)
            self.page.goto(self.fixture_url, wait_until="domcontentloaded", timeout=15_000)
            self.page.wait_for_function(
                "document.documentElement && document.documentElement.dataset.benchmarkReady === 'true'",
                timeout=15_000,
            )
            self.initial_observation = self._observe_locked("initial")
            write_json(self.evidence / "browser_ready.json", {
                "protocol": PROTOCOL, "fixture_origin": self.fixture_url,
                "viewport": self.viewport, "ready": True, "started": self.started,
            })
        except Exception:
            self.close()
            raise

    def _request_guard(self, route: Any) -> None:
        """Allow the fixture itself to load only same-origin resources."""
        try:
            target = urlsplit(route.request.url)
            origin = urlsplit(self.fixture_url)
            if (target.scheme, target.hostname, target.port or 80) != (origin.scheme, origin.hostname, origin.port or 80):
                route.abort()
            else:
                route.continue_()
        except Exception:
            # Browser event callbacks cannot reliably abort after an exception;
            # the page remains unusable and the harness classifies it as infra.
            route.abort()

    def _visible_handles(self) -> list[dict[str, Any]]:
        """Extract metadata for rendered controls without exposing source/JS."""
        script = """
        (el => {
          const r = el.getBoundingClientRect(), s = getComputedStyle(el);
          let visible = !!(r.width && r.height) && s.visibility !== 'hidden' && s.display !== 'none' &&
             Number(s.opacity) !== 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
          let left=Math.max(r.left,0), right=Math.min(r.right,innerWidth), top=Math.max(r.top,0), bottom=Math.min(r.bottom,innerHeight);
          for (let p=el.parentElement;p;p=p.parentElement) {
              const ps=getComputedStyle(p),pr=p.getBoundingClientRect();
              if (Number(ps.opacity)===0 || ps.visibility==='hidden' || ps.display==='none') visible=false;
              if (['hidden','scroll','auto','clip'].includes(ps.overflowX)) {left=Math.max(left,pr.left);right=Math.min(right,pr.right);}
              if (['hidden','scroll','auto','clip'].includes(ps.overflowY)) {top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}
          }
          visible = visible && right>left && bottom>top;
          const hit=visible ? document.elementFromPoint((left+right)/2,(top+bottom)/2) : null;
          visible = visible && !!hit && (hit===el || el.contains(hit));
          const labels = [...(el.labels || [])].map(l => l.innerText).join(' ');
          const text = (el.getAttribute('aria-label') || labels || el.innerText || el.getAttribute('alt') || '').trim();
          return {tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
                  label: text.slice(0, 200), type: el.getAttribute('type') || '',
                  context: (el.closest('article,tr')?.innerText || '').slice(0,500),
                  checked: Boolean(el.checked), disabled: Boolean(el.disabled),
                  value: (el.tagName.toLowerCase() === 'input' && el.type !== 'password') ? (el.value || '').slice(0, 200) : '',
                  options: el.tagName === 'SELECT' ? [...el.options].filter(o => !o.hidden).map(o => o.label) : [],
                  x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height), visible};
        })
        """
        for element, _ in self.handle_map.values():
            element.dispose()
        self.handle_map = {}
        for element in self.page.locator("button,input,select,textarea,a,canvas,img").element_handles():
            row = element.evaluate(script)
            if row.get('visible') and row.get('tag') in ALLOWED_TAGS:
                handle = secrets.token_hex(12)
                row['handle'] = handle
                self.handle_map[handle] = element, row
            else:
                element.dispose()
        return [row for _, row in self.handle_map.values()]

    def rendered_regions(self) -> list[dict[str, Any]]:
        return self.page.evaluate('''() => {
            const result=[];
            for (const e of document.body.querySelectorAll('*')) {
                if (['SCRIPT','STYLE','NOSCRIPT','OPTION'].includes(e.tagName)) continue;
                const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                if (!r.width || !r.height || r.bottom<=0 || r.right<=0 || r.top>=innerHeight || r.left>=innerWidth ||
                    s.display==='none' || s.visibility==='hidden' || Number(s.opacity)===0) continue;
                let left=Math.max(r.left,0),right=Math.min(r.right,innerWidth),top=Math.max(r.top,0),bottom=Math.min(r.bottom,innerHeight),hidden=false;
                for(let p=e.parentElement;p;p=p.parentElement){
                    const ps=getComputedStyle(p),pr=p.getBoundingClientRect();
                    if(Number(ps.opacity)===0 || ps.visibility==='hidden' || ps.display==='none') hidden=true;
                    if(['hidden','scroll','auto','clip'].includes(ps.overflowX)){left=Math.max(left,pr.left);right=Math.min(right,pr.right);}
                    if(['hidden','scroll','auto','clip'].includes(ps.overflowY)){top=Math.max(top,pr.top);bottom=Math.min(bottom,pr.bottom);}
                }
                if(hidden || right<=left || bottom<=top)continue;
                const hit=document.elementFromPoint((left+right)/2,(top+bottom)/2);
                if(!hit || !(hit===e || e.contains(hit)))continue;
                const scrollable=e.scrollHeight>e.clientHeight && ['auto','scroll'].includes(s.overflowY);
                const isRow=e.tagName==='TR' || [...e.children].some(c=>c.tagName==='INPUT' && c.type==='checkbox');
                const text=isRow ? e.innerText : [...e.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join(' ').trim();
                if (scrollable || text) result.push({text:text.slice(0,500),scrollable,x:r.x,y:r.y,width:r.width,height:r.height});
            }
            return result.slice(0,1000);
        }''')

    def _observe_locked(self, phase: str | None = None) -> dict[str, Any]:
        if self.page is None or self.closed:
            raise RuntimeError("browser is closed")
        # Let the fixture settle its own event handlers before capturing.
        self.page.wait_for_timeout(30)
        controls = self._visible_handles()
        regions = self.rendered_regions()
        png = self.page.screenshot(type="png", animations="disabled")
        self.sequence += 1
        observation_id = f"o{self.sequence:06d}"
        row: dict[str, Any] = {
            "protocol": PROTOCOL, "observation_id": observation_id, "timestamp_utc": now(),
            "viewport": self.viewport,
            "text": _safe_text('\n'.join(row['text'] for row in regions if row['text'])),
            "controls": controls, "png_sha256": sha256(png),
            "regions": regions,
            "png_bytes": len(png), "page_title": _safe_text(self.page.title(), 512),
        }
        if phase:
            if phase not in {"initial", "decisive", "final"}:
                raise ValueError("screenshot phase must be initial, decisive, or final")
            path = self.evidence / "screenshots" / f"{phase}.png"
            if path.exists():
                # Repeated phase captures are preserved; receipt remains bound
                # to the first capture and cannot be replaced by a candidate.
                path = self.evidence / "screenshots" / f"{phase}-{self.sequence:06d}.png"
            path.write_bytes(png)
            row["phase"] = phase
            row["capture_sha256"] = sha256(png)
            self.receipts.append({"phase": phase, "observation_id": observation_id,
                                  "sha256": sha256(png), "bytes": len(png),
                                  "viewport": self.viewport, "path": str(path), "captured_at_utc": now()})
        self.events.append({"kind": "observe", "observation_id": observation_id,
                            "phase": phase, "sha256": sha256(png), "timestamp_utc": now()})
        self.flush()
        return row | {"png_base64": base64.b64encode(png).decode("ascii")}

    def observe(self, phase: str | None = None) -> dict[str, Any]:
        with self.lock:
            return self._observe_locked(phase)

    def _locator(self, handle: str) -> tuple[Any, dict[str, Any]]:
        if not isinstance(handle, str) or handle not in self.handle_map:
            raise ValueError("unknown or stale rendered control handle; observe again")
        locator, row = self.handle_map[handle]
        if not locator.is_visible():
            raise ValueError("control is no longer visible; observe again")
        return locator, row

    def action(self, body: dict[str, Any]) -> dict[str, Any]:
        operation = body.get("operation")
        allowed = {
            'click': {'operation', 'handle', 'x', 'y'}, 'type': {'operation', 'handle', 'text'},
            'fill': {'operation', 'handle', 'text'}, 'key': {'operation', 'key'},
            'select': {'operation', 'handle', 'label'}, 'scroll': {'operation', 'dx', 'dy', 'x', 'y'},
            'drag': {'operation', 'points'}, 'wait': {'operation', 'seconds', 'visible_text'},
            'screenshot': {'operation', 'phase'},
        }
        if operation not in allowed or set(body) - allowed[operation]:
            raise ValueError("unsupported operation")
        with self.lock:
            if operation == "screenshot":
                if body.get('phase') not in {'initial', 'decisive', 'final'}:
                    raise ValueError('screenshot phase is required')
                if body['phase'] == 'initial':
                    return self.initial_observation
                result = self._observe_locked(body.get("phase"))
                return result
            if operation in {"click", "type", "fill", "select"}:
                locator, row = (self._locator(body.get('handle')) if 'handle' in body else (None, {}))
                tag = row.get("tag")
                if operation == "click":
                    if locator:
                        locator.click(timeout=5000)
                    else:
                        x, y = self.point(body)
                        self.page.mouse.click(x, y)
                elif operation in {"type", "fill"}:
                    if tag not in {"input", "textarea"} or row.get("type") == "password":
                        raise ValueError("type/fill requires a visible non-password input")
                    value = body.get("text")
                    if not isinstance(value, str) or len(value) > 2000:
                        raise ValueError("text must be a string of at most 2000 characters")
                    if operation == "fill":
                        locator.fill(value, timeout=5000)
                    else:
                        locator.click(timeout=5000)
                        self.page.keyboard.type(value, delay=0)
                elif operation == "select":
                    if tag != "select":
                        raise ValueError("select requires a native select control")
                    value = body.get("label", body.get("value"))
                    if not isinstance(value, str) or len(value) > 300:
                        raise ValueError("select label/value is required")
                    locator.select_option(label=value, timeout=5000)
            elif operation == "key":
                key = body.get("key")
                if not isinstance(key, str) or key not in ALLOWED_KEYS and not (len(key) == 1 and key.isprintable()):
                    raise ValueError("unsupported browser key")
                self.page.keyboard.press(key)
            elif operation == "scroll":
                try:
                    dx, dy = int(body.get("dx", 0)), int(body.get("dy", 0))
                except Exception as exc:
                    raise ValueError("scroll deltas must be integers") from exc
                if abs(dx) > 2000 or abs(dy) > 2000:
                    raise ValueError("scroll delta exceeds bound")
                x, y = body.get("x"), body.get("y")
                if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                    raise ValueError("scroll requires viewport x/y")
                if not (0 <= x <= self.viewport["width"] and 0 <= y <= self.viewport["height"]):
                    raise ValueError("scroll point outside viewport")
                self.page.mouse.move(x, y)
                self.page.mouse.wheel(dx, dy)
            elif operation == "drag":
                points = body.get('points')
                if not isinstance(points, list) or not 2 <= len(points) <= 100:
                    raise ValueError('drag requires 2 to 100 bounded viewport points')
                points = [self.point(p) for p in points]
                self.page.mouse.move(*points[0])
                self.page.mouse.down()
                try:
                    for point in points[1:]:
                        self.page.mouse.move(*point, steps=8)
                finally:
                    self.page.mouse.up()
            elif operation == "wait":
                seconds = float(body.get("seconds", 0.2))
                if seconds < 0 or seconds > MAX_WAIT:
                    raise ValueError("wait duration outside bound")
                visible_text = body.get("visible_text")
                if visible_text is not None:
                    if not isinstance(visible_text, str) or len(visible_text) > 500:
                        raise ValueError("visible_text must be a short string")
                    self.page.get_by_text(visible_text, exact=False).first.wait_for(state="visible", timeout=int(max(seconds, 0.1) * 1000))
                else:
                    self.page.wait_for_timeout(int(seconds * 1000))
            self.sequence += 1
            event = {"kind": "action", "sequence": self.sequence, "operation": operation,
                     "timestamp_utc": now(), "request": {k: v for k, v in body.items() if k not in {"text"}},
                     "accepted": True}
            self.events.append(event)
            return {"protocol": PROTOCOL, "action_sequence": self.sequence,
                    "accepted": True, "observation": self._observe_locked()}

    def rejected(self, path: str, body: dict[str, Any] | None, error: str) -> None:
        self.events.append({'kind': 'rejected', 'timestamp_utc': now(), 'path': path,
                            'operation': body.get('operation') if isinstance(body, dict) else None,
                            'error': error[:300]})
        self.flush()

    def point(self, value: dict[str, Any]) -> tuple[float, float]:
        x, y = value.get('x'), value.get('y')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (x, y)):
            raise ValueError('viewport coordinates must be numeric')
        if not 0 <= x < self.viewport['width'] or not 0 <= y < self.viewport['height']:
            raise ValueError('point outside viewport')
        return x, y

    def flush(self) -> None:
        write_json(self.evidence / 'browser_receipts.json', {
            'protocol': PROTOCOL, 'viewport': self.viewport, 'receipts': self.receipts,
        })
        write_json(self.evidence / 'browser_events.json', {'protocol': PROTOCOL, 'events': self.events})

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            finished = now()
            try:
                if self._context:
                    self._context.close()
            finally:
                try:
                    if self._browser:
                        self._browser.close()
                finally:
                    if self._playwright:
                        self._playwright.stop()
            write_json(self.evidence / "browser_receipts.json", {
                "protocol": PROTOCOL, "fixture_origin": self.fixture_url,
                "viewport": self.viewport, "started": self.started, "finished": finished,
                "receipts": self.receipts,
            })
            (self.evidence / "browser_events.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in self.events),
                encoding="utf-8",
            )
            write_json(self.evidence / "browser_closed.json", {"protocol": PROTOCOL, "closed": True, "finished": finished})
            if self.short_tmp:
                shutil.rmtree(self.short_tmp, ignore_errors=True)


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "EvaluatorGuiControl/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:
        pass

    @property
    def controller(self) -> BrowserController:
        return self.server.controller  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-GUI-Control-Token", ""), self.server.token)  # type: ignore[attr-defined]

    def _send(self, status: int, value: Any) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "forbidden"})
            return
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(200, {"ready": not self.controller.closed, "protocol": PROTOCOL, "case_id": self.server.case_id})  # type: ignore[attr-defined]
        elif path == "/v1/observe":
            try:
                self._send(200, self.controller.observe())
            except Exception as exc:
                self._send(503, {"error": type(exc).__name__ + ": " + str(exc)[:300]})
        else:
            self.controller.rejected(path, None, 'unsupported endpoint')
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            self._send(403, {"error": "forbidden"})
            return
        path = urlsplit(self.path).path
        body = None
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_BODY:
                raise ValueError("request body exceeds limit")
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("request must be a JSON object")
            if path == "/v1/action":
                self._send(200, self.controller.action(body))
            else:
                self.controller.rejected(path, body, 'unsupported endpoint')
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self.controller.rejected(path, body, type(exc).__name__ + ': ' + str(exc))
            self._send(400, {"error": type(exc).__name__ + ": " + str(exc)[:300]})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-url", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--viewport", required=True, help="JSON object with width/height")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--browser-session", required=True)
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    viewport = json.loads(args.viewport)
    controller = BrowserController(args.fixture_url, viewport, args.evidence, token, args.browser_session)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    server.controller, server.token, server.case_id = controller, token, args.case_id
    try:
        controller.start()
        print(json.dumps({"ready": True, "protocol": PROTOCOL, "url": f"http://127.0.0.1:{server.server_port}", "token": token}), flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        (args.evidence / 'browser_error.log').write_text(traceback.format_exc(), encoding='utf-8')
        write_json(args.evidence / "browser_error.json", {"protocol": PROTOCOL, "ready": False, "error": type(exc).__name__ + ": " + str(exc)[:500]})
        print(json.dumps({"ready": False, "protocol": PROTOCOL, "error": type(exc).__name__ + ": " + str(exc)[:500]}), flush=True)
        return 70
    finally:
        server.server_close()
        controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
