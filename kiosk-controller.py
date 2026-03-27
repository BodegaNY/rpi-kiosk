#!/usr/bin/env python3
"""Kiosk controller: CDP-based URL rotation with web control panel on port 8088."""

import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket

CONTROL_PORT = 8088
CDP_BASE = "http://localhost:9222"
CONFIG_PATH = "/home/rpi3b/.kiosk-config.json"

BACKYARD_BASE = "http://100.123.231.73:8089"

VIEWS = {
    "dakboard": {
        "name": "Dakboard",
        "url": "https://dakboard.com/app/screenPredefined?p=7670732593b74717b72fedf004de3640",
    },
    "camera": {
        "name": "Camera",
        "url": "file:///home/rpi3b/cam-viewer.html",
    },
    "backyard": {
        "name": "Backyard",
        "url": None,
    },
}
VIEW_ORDER = ["dakboard", "camera", "backyard"]

META_FLAGS = ("relative", "iso", "conf", "bbox", "model", "size")

# RLock: /api/status holds the lock and calls get_view_url -> build_backyard_query (nested lock).
state_lock = threading.RLock()
nav_lock = threading.Lock()
state = {
    "current_view": "dakboard",
    "rotate": True,
    "durations": {"dakboard": 30, "camera": 30, "backyard": 30},
    "backyard_layout": "list",
    "backyard_meta": ["relative"],
}


def encode_backyard_query(layout, meta):
    """URL query for backyard view; meta may be list or comma string (no lock)."""
    if isinstance(meta, str):
        meta = [x.strip() for x in meta.split(",") if x.strip()]
    if not meta:
        meta = ["relative"]
    return urllib.parse.urlencode([("layout", layout), ("meta", ",".join(meta))])


def build_backyard_query():
    with state_lock:
        layout = state.get("backyard_layout", "list")
        meta = state.get("backyard_meta", ["relative"])
    return encode_backyard_query(layout, meta)


def get_view_url(view_key):
    if view_key == "backyard":
        return f"{BACKYARD_BASE}/?{build_backyard_query()}"
    u = VIEWS.get(view_key, {}).get("url")
    if u is None:
        raise ValueError(view_key)
    return u


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
            if "backyard_layout" in saved and saved["backyard_layout"] in ("list", "highlight_recent"):
                state["backyard_layout"] = saved["backyard_layout"]
            if "backyard_meta" in saved:
                bm = saved["backyard_meta"]
                if isinstance(bm, str):
                    bm = [x.strip() for x in bm.split(",") if x.strip()]
                if isinstance(bm, list):
                    state["backyard_meta"] = [x for x in bm if x in META_FLAGS]
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save_config():
    with state_lock:
        data = {
            "rotate": state["rotate"],
            "durations": dict(state["durations"]),
            "backyard_layout": state.get("backyard_layout", "list"),
            "backyard_meta": list(state.get("backyard_meta", ["relative"])),
        }
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def get_ws_url():
    """Pick the kiosk *page* WebSocket, not the browser-level target (Chromium lists browser first)."""
    data = urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5).read()
    tabs = json.loads(data)
    non_browser = [
        t for t in tabs
        if t.get("type") != "browser" and "webSocketDebuggerUrl" in t
    ]
    pages = [t for t in non_browser if t.get("type") == "page"]
    for tab in pages:
        url = tab.get("url") or ""
        if url.startswith(("http://", "https://", "file://", "about:blank")):
            return tab["webSocketDebuggerUrl"]
    if pages:
        return pages[0]["webSocketDebuggerUrl"]
    if non_browser:
        return non_browser[0]["webSocketDebuggerUrl"]
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
        return False, "unknown view"
    with nav_lock:
        try:
            url = get_view_url(view_key)
            cdp_navigate(url)
            with state_lock:
                state["current_view"] = view_key
            return True, None
        except Exception as e:
            print(f"kiosk-controller: CDP navigate failed: {e}", file=sys.stderr)
            return False, str(e)


def rotation_loop():
    while True:
        try:
            get_ws_url()
            break
        except Exception:
            time.sleep(2)

    with state_lock:
        initial = state["current_view"]
    ok, _err = switch_to(initial)
    if not ok:
        print("kiosk-controller: initial switch_to failed", file=sys.stderr)

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
            ok, _err = switch_to(next_view)
            if not ok:
                print("kiosk-controller: rotation switch_to failed", file=sys.stderr)


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
.w{max-width:32rem;margin:0 auto}
h1{text-align:center;font-size:1.3rem;color:var(--ac);margin-bottom:1.5rem}
.c{background:var(--card);border-radius:.75rem;padding:1.25rem;margin-bottom:1rem}
.lb{font-size:.8rem;color:#888;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.75rem}
.st{font-size:1.5rem;font-weight:700}
.st.dakboard{color:#5dade2} .st.camera{color:#58d68d} .st.backyard{color:#bb8fce}
.r{display:flex;gap:.6rem;flex-wrap:wrap}
.b{flex:1;min-width:5rem;padding:.9rem;border:none;border-radius:.5rem;font-size:1rem;
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
.dr select{width:100%;padding:.5rem;border:1px solid #333;border-radius:.4rem;
           background:var(--bg);color:#eaeaea;font-size:.9rem}
.meta{font-size:.85rem;color:#aaa;display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-top:.5rem}
.meta label{display:flex;align-items:center;gap:.25rem;cursor:pointer}
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
    <button class="b" id="bb" onclick="sw('backyard')">Backyard</button>
  </div>
</div>
<div class="c">
  <div class="lb">Auto-rotate views</div>
  <div class="fb">
    <span id="rt">Disabled</span>
    <label class="tg" title="When enabled, cycles Dakboard → Camera → Backyard"><input type="checkbox" id="ro" onchange="tr()"><span class="tk"></span></label>
  </div>
  <p style="font-size:.78rem;color:#666;margin-top:.5rem;line-height:1.35">Toggle right (on) = rotation <strong>enabled</strong>. Left = <strong>disabled</strong> (stay on current view until you press a view button).</p>
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
  <div class="dr fb">
    <span>Backyard</span>
    <div><input id="db" type="number" min="5" max="3600" value="30"
         onchange="sd('backyard',this.value)"> s</div>
  </div>
</div>
<div class="c">
  <div class="lb">Backyard gallery</div>
  <div class="dr">
    <span style="display:block;margin-bottom:.35rem">Layout</span>
    <select id="bl" onchange="sb()">
      <option value="list">List</option>
      <option value="highlight_recent">Highlight recent</option>
    </select>
  </div>
  <div class="meta" id="bm">
    <span style="width:100%;margin-bottom:.25rem">Metadata on URL</span>
    <label><input type="checkbox" data-m="relative"> Relative</label>
    <label><input type="checkbox" data-m="iso"> Timestamp</label>
    <label><input type="checkbox" data-m="conf"> Confidence</label>
    <label><input type="checkbox" data-m="bbox"> Boxes</label>
    <label><input type="checkbox" data-m="model"> Model</label>
    <label><input type="checkbox" data-m="size"> Size</label>
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
    $('ro').checked=d.rotate; $('rt').textContent=d.rotate?'Enabled':'Disabled';
    $('dd').value=d.durations.dakboard; $('dc').value=d.durations.camera; $('db').value=d.durations.backyard;
    $('bd').className='b'+(d.current_view==='dakboard'?' on':'');
    $('bc').className='b'+(d.current_view==='camera'?' on':'');
    $('bb').className='b'+(d.current_view==='backyard'?' on':'');
    $('bl').value=d.backyard_layout||'list';
    const set=d.backyard_meta||['relative'];
    document.querySelectorAll('#bm input[data-m]').forEach(cb=>{
      cb.checked=set.indexOf(cb.dataset.m)>=0;
    });
  }).catch(()=>{});
}
function handleApi(promise,label){
  promise.then(d=>{
    if(d&&d.ok===false){
      alert((label||'Error')+': '+(d.error||d.detail||'failed'));
    }
    rf();
  }).catch(()=>{alert((label||'Request')+' failed (network)');rf();});
}
function sw(v){handleApi(api('POST','switch',{view:v}),'Switch')}
function tr(){handleApi(api('POST','rotate'),'Rotate')}
function sd(v,s){handleApi(api('POST','duration',{view:v,seconds:parseInt(s)}),'Duration')}
function sb(){
  const layout=$('bl').value;
  const meta=[];
  document.querySelectorAll('#bm input[data-m]').forEach(cb=>{
    if(cb.checked)meta.push(cb.dataset.m);
  });
  handleApi(api('POST','backyard',{layout,meta}),'Backyard');
}
document.querySelectorAll('#bm input[data-m]').forEach(cb=>{
  cb.addEventListener('change',sb);
});
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
                layout = state.get("backyard_layout", "list")
                meta = list(state.get("backyard_meta", ["relative"]))
                d = {
                    "current_view": cv,
                    "current_view_name": VIEWS.get(cv, {}).get("name", cv),
                    "rotate": state["rotate"],
                    "durations": dict(state["durations"]),
                    "backyard_layout": layout,
                    "backyard_meta": meta,
                }
            if "backyard" in VIEWS:
                d["backyard_url"] = f"{BACKYARD_BASE}/?{encode_backyard_query(layout, meta)}"
            else:
                d["backyard_url"] = ""
            self._json(d)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/switch":
            b = self._body()
            view = b.get("view")
            if not view:
                self._json({"ok": False, "error": "missing view"}, 400)
            else:
                ok, err = switch_to(view)
                if ok:
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "navigate failed", "detail": err or ""}, 400)

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

        elif self.path == "/api/backyard":
            b = self._body()
            layout = b.get("layout")
            meta = b.get("meta")
            if layout not in ("list", "highlight_recent"):
                self._json({"ok": False, "error": "invalid layout"}, 400)
                return
            if not isinstance(meta, list):
                self._json({"ok": False, "error": "meta must be a list"}, 400)
                return
            clean = [x for x in meta if x in META_FLAGS]
            if not clean:
                clean = ["relative"]
            with state_lock:
                state["backyard_layout"] = layout
                state["backyard_meta"] = clean
            save_config()
            with state_lock:
                on_backyard = state.get("current_view") == "backyard"
            if on_backyard:
                ok, err = switch_to("backyard")
                if not ok:
                    self._json({"ok": False, "error": "navigate failed", "detail": err or ""}, 400)
                    return
            self._json({"ok": True})
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