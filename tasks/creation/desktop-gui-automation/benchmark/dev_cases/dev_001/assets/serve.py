#!/usr/bin/env python3
import argparse, json, random, secrets, struct, threading, zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CASE_ID = "dev_001"
ROOT = Path(__file__).resolve().parent
LOCK = threading.Lock()
RECORD = {"state": {}, "trace": [], "decisive_snapshots": [], "record_count": 0}
CAPTCHA = "".join(random.SystemRandom().choice("23456789") for _ in range(5))
VERIFY_CODE = "".join(random.SystemRandom().choice("0123456789") for _ in range(6))

DIGITS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111",
    "3": "111001111001111", "4": "101101111001001", "5": "111100111001111",
    "6": "111100111101111", "7": "111001010010010", "8": "111101111101111",
    "9": "111101111001111"
}

def captcha_png():
    w, h = 220, 72
    rng = random.Random(int(CAPTCHA))
    pixels = [[[246 + rng.randrange(0, 9), 247, 244] for _ in range(w)] for _ in range(h)]
    for _ in range(38):
        x0, y0, length = rng.randrange(w), rng.randrange(h), rng.randrange(8, 42)
        color = [rng.randrange(70, 170), rng.randrange(80, 170), rng.randrange(80, 170)]
        for i in range(length):
            x, y = (x0 + i) % w, min(h - 1, max(0, y0 + (i // 9) - 2))
            pixels[y][x] = color
    for n, ch in enumerate(CAPTCHA):
        mask = DIGITS[ch]
        ox, oy, scale = 18 + n * 40 + rng.randrange(-2, 3), 12 + rng.randrange(-3, 4), 8
        color = [20 + rng.randrange(40), 46 + rng.randrange(50), 60 + rng.randrange(50)]
        for row in range(5):
            for col in range(3):
                if mask[row * 3 + col] == "1":
                    for yy in range(scale):
                        for xx in range(scale):
                            x, y = ox + col * scale + xx, oy + row * scale + yy
                            if 0 <= x < w and 0 <= y < h and (xx + yy + n) % 7:
                                pixels[y][x] = color
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in pixels)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")

class Handler(BaseHTTPRequestHandler):
    server_version = "BenchmarkFixture/2"
    def log_message(self, *_): pass
    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ('/health', '/__evaluator__/state') and self.headers.get('Cookie', '') != f'evaluator_browser_session={self.server.browser_session}':return self.send(403,{'error':'browser session required'})
        if path == "/": self.send(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/health": self.send(200, {"ready": True, "case_id": CASE_ID})
        elif path == "/captcha.png": self.send(200, captcha_png(), "image/png")
        elif path == "/verification-code": self.send(200, {"code": VERIFY_CODE})
        elif path == "/__evaluator__/state":
            if self.headers.get("X-Evaluator-Token") != self.server.evaluator_token: return self.send(403, {"error": "forbidden"})
            with LOCK: payload = json.loads(json.dumps(RECORD))
            payload.update({"case_id": CASE_ID, "runtime_challenge": {"captcha_answer": CAPTCHA, "verification_code": VERIFY_CODE}})
            self.send(200, payload)
        else: self.send(404, {"error": "not found"})
    def do_POST(self):
        if self.headers.get("Cookie", "") != f"evaluator_browser_session={self.server.browser_session}":
            return self.send(403, {"error": "browser session required"})
        size = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
        try: data = json.loads(self.rfile.read(size) or b"{}")
        except Exception: return self.send(400, {"error": "invalid json"})
        if self.path == "/record":
            if self.headers.get("Cookie", "") != f"evaluator_browser_session={self.server.browser_session}":
                return self.send(403, {"error": "browser session required"})
            with LOCK:
                RECORD["state"] = data.get("state", {})
                RECORD["trace"] = data.get("trace", [])
                RECORD["decisive_snapshots"] = data.get("decisive_snapshots", [])
                RECORD["record_count"] += 1
            self.send(200, {"ok": True})
        elif self.path == "/captcha-check": self.send(200, {"ok": str(data.get("answer", "")).strip() == CAPTCHA})
        else: self.send(404, {"error": "not found"})

def main():
    p = argparse.ArgumentParser(); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=0); p.add_argument("--evaluator-token", required=True); p.add_argument("--browser-session", required=True)
    args = p.parse_args(); server = ThreadingHTTPServer((args.host, args.port), Handler); server.evaluator_token = args.evaluator_token; server.browser_session = args.browser_session
    print(json.dumps({"ready": True, "case_id": CASE_ID, "url": f"http://{args.host}:{server.server_port}"}), flush=True)
    server.serve_forever()
if __name__ == "__main__": main()
