#!/usr/bin/env python3
"""Kiosk controller: CDP-based URL rotation with web control panel on port 8088."""

import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket

CONTROL_PORT = 8088
CDP_BASE = "http://localhost:9222"
CONFIG_PATH = "/home/rpi3b/.kiosk-config.json"

VIEWS = {
    "dakboard": {
        "name": "Dakboard",
        "url": "https://dakboard.com/app/screenPredefined?p=7670732593b74717b72fedf004de3640",
    },
    "camera": {
        "name": "Camera",
        "url": "file:///home/rpi3b/cam-viewer.html",
    },
}
VIEW_ORDER = ["dakboard", "camera"]

state_lock = threading.Lock()
nav_lock = threading.Lock()
state = {
    "current_view": "dakboard",
    "rotate": True,
    "durations": {"dakboard": 30, "camera": 30},
}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        with state_lock:
            if "rotate" in saved:
                state["rotate"] = bool(saved["rotate"])
            if "durations" in saved:
                for k in VIEWS:
                    if k in saved["durations"]:
                        state["durations"][k] = max(5, min(3600, int(saved["durations"][k])))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save_config():
    with state_lock:
        data = {"rotate": state["rotate"], "durations": dict(state["durations"])}
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def get_ws_url():
    data = urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5).read()
    tabs = json.loads(data)
    for tab in tabs:
        if "webSocketDebuggerUrl" in tab:
            return tab["webSocketDebuggerUrl"]
    raise RuntimeError("No debuggable tab found")


def cdp_navigate(url):
    ws_url = get_ws_url()
    ws = websocket.create_connection(ws_url, timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        ws.recv()
    finally:
        ws.close()


def switch_to(view_key):
    if view_key not in VIEWS:
        return False
    with nav_lock:
        try:
            cdp_navigate(VIEWS[view_key]["url"])
            with state_lock:
                state["current_view"] = view_key
            return True
        except Exception:
            return False


def rotation_loop():
    while True:
        try:
            get_ws_url()
            break
        except Exception:
            time.sleep(2)

    with state_lock:
        initial = state["current_view"]
    switch_to(initial)

    while True:
        with state_lock:
            rotating = state["rotate"]
        if not rotating:
            time.sleep(1)
            continue

        with state_lock:
            current = state["current_view"]
            duration = state["durations"].get(current, 30)

        for _ in range(duration):
            time.sleep(1)
            with state_lock:
                if not state["rotate"] or state["current_view"] != current:
                    break

        with state_lock:
            should_switch = state["rotate"] and state["current_view"] == current
            if should_switch:
                idx = VIEW_ORDER.index(current) if current in VIEW_ORDER else -1
                next_view = VIEW_ORDER[(idx + 1) % len(VIEW_ORDER)]

        if should_switch:
            switch_to(next_view)


# ---------------------------------------------------------------------------
# Embedded control-panel HTML
# ---------------------------------------------------------------------------

CONTROL_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kiosk Control</title>
<style>
:root{--ac:#e94560;--bg:#1a1a2e;--card:#16213e;--inp:#0f3460}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     background:var(--bg);color:#eaeaea;min-height:100vh;padding:1.25rem}
.w{max-width:28rem;margin:0 auto}
h1{text-align:center;font-size:1.3rem;color:var(--ac);margin-bottom:1.5rem}
.c{background:var(--card);border-radius:.75rem;padding:1.25rem;margin-bottom:1rem}
.lb{font-size:.8rem;color:#888;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.75rem}
.st{font-size:1.5rem;font-weight:700}
.st.dakboard{color:#5dade2} .st.camera{color:#58d68d}
.r{display:flex;gap:.6rem}
.b{flex:1;padding:.9rem;border:none;border-radius:.5rem;font-size:1rem;
   font-weight:600;cursor:pointer;background:var(--inp);color:#ccc;transition:.15s}
.b:active{transform:scale(.97)} .b.on{background:var(--ac);color:#fff}
.fb{display:flex;align-items:center;justify-content:space-between}
.tg{position:relative;width:3.2rem;height:1.75rem;flex-shrink:0}
.tg input{opacity:0;width:0;height:0}
.tk{position:absolute;inset:0;background:#333;border-radius:1rem;cursor:pointer;transition:.25s}
.tk::before{content:"";position:absolute;width:1.35rem;height:1.35rem;left:.2rem;bottom:.2rem;
            background:#ddd;border-radius:50%;transition:.25s}
.tg input:checked+.tk{background:var(--ac)}
.tg input:checked+.tk::before{transform:translateX(1.45rem)}
.dr{margin-top:.75rem}
.dr input{width:5rem;padding:.5rem;border:1px solid #333;border-radius:.4rem;
          background:var(--bg);color:#eaeaea;font-size:1rem;text-align:center}
.mu{text-align:center;color:#555;font-size:.75rem;margin-top:1rem}
</style></head><body>
<div class="w">
<h1>Hallway Kiosk</h1>
<div class="c">
  <div class="lb">Now Showing</div>
  <div class="st" id="vn">---</div>
</div>
<div class="c">
  <div class="lb">Switch View</div>
  <div class="r">
    <button class="b" id="bd" onclick="sw('dakboard')">Dakboard</button>
    <button class="b" id="bc" onclick="sw('camera')">Camera</button>
  </div>
</div>
<div class="c">
  <div class="lb">Auto-Rotate</div>
  <div class="fb">
    <span id="rt">Off</span>
    <label class="tg"><input type="checkbox" id="ro" onchange="tr()"><span class="tk"></span></label>
  </div>
  <div class="dr fb">
    <span>Dakboard</span>
    <div><input id="dd" type="number" min="5" max="3600" value="30"
         onchange="sd('dakboard',this.value)"> s</div>
  </div>
  <div class="dr fb">
    <span>Camera</span>
    <div><input id="dc" type="number" min="5" max="3600" value="30"
         onchange="sd('camera',this.value)"> s</div>
  </div>
</div>
<p class="mu">rpi3b-hallway-kiosk</p>
</div>
<script>
const $=id=>document.getElementById(id),
      api=(m,p,b)=>fetch('/api/'+p,{method:m,
        headers:b?{'Content-Type':'application/json'}:{},
        body:b?JSON.stringify(b):undefined}).then(r=>r.json());

function rf(){
  api('GET','status').then(d=>{
    const v=$('vn'); v.textContent=d.current_view_name; v.className='st '+d.current_view;
    $('ro').checked=d.rotate; $('rt').textContent=d.rotate?'On':'Off';
    $('dd').value=d.durations.dakboard; $('dc').value=d.durations.camera;
    $('bd').className='b'+(d.current_view==='dakboard'?' on':'');
    $('bc').className='b'+(d.current_view==='camera'?' on':'');
  }).catch(()=>{});
}
function sw(v){api('POST','switch',{view:v}).then(rf)}
function tr(){api('POST','rotate').then(rf)}
function sd(v,s){api('POST','duration',{view:v,seconds:parseInt(s)}).then(rf)}
rf(); setInterval(rf,3000);
</script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        if self.path == "/":
            body = CONTROL_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            with state_lock:
                cv = state["current_view"]
                d = {
                    "current_view": cv,
                    "current_view_name": VIEWS.get(cv, {}).get("name", cv),
                    "rotate": state["rotate"],
                    "durations": dict(state["durations"]),
                }
            self._json(d)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/switch":
            b = self._body()
            view = b.get("view")
            if view and switch_to(view):
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "invalid view or navigate failed"}, 400)

        elif self.path == "/api/rotate":
            with state_lock:
                state["rotate"] = not state["rotate"]
                r = state["rotate"]
            save_config()
            self._json({"ok": True, "rotate": r})

        elif self.path == "/api/duration":
            b = self._body()
            view = b.get("view")
            seconds = b.get("seconds")
            if view in VIEWS and isinstance(seconds, int) and 5 <= seconds <= 3600:
                with state_lock:
                    state["durations"][view] = seconds
                save_config()
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "invalid view or duration"}, 400)
        else:
            self.send_error(404)


def main():
    load_config()
    threading.Thread(target=rotation_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    print(f"Kiosk controller on :{CONTROL_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
