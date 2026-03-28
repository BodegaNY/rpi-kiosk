#!/usr/bin/env python3
"""Kiosk controller: CDP-based URL rotation with web control panel on port 8088."""

import gzip
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket
try:
    from google.transit import gtfs_realtime_pb2
except ImportError:
    gtfs_realtime_pb2 = None
try:
    from google.protobuf.message import DecodeError
except Exception:
    DecodeError = Exception

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
    "mta": {
        "name": "MTA",
        "url": f"http://127.0.0.1:{CONTROL_PORT}/mta",
    },
}
VIEW_ORDER = ["dakboard", "camera", "backyard", "mta"]

META_FLAGS = ("relative", "iso", "conf", "bbox", "model", "size")

# Matches classifier gallery filter (?class=); empty = show all types.
BACKYARD_FILTER_CLASSES = frozenset({"bird", "cat", "dog", "person"})

MTA_FEEDS = {
    "gtfs-1234567": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs",
    "gtfs-ace": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace",
    "gtfs-bdfm": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm",
    "gtfs-nqrw": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw",
}
MTA_CACHE_SECONDS = 20
MTA_MAX_MINUTES = 90
MTA_ROUTE_COLORS = {
    "A": "#0039A6", "B": "#FF6319", "C": "#0039A6", "D": "#FF6319", "E": "#0039A6",
    "N": "#FCCC0A", "Q": "#FCCC0A", "R": "#FCCC0A", "W": "#FCCC0A",
    "1": "#EE352E", "2": "#EE352E", "3": "#EE352E",
}
MTA_STATIONS = {
    "7av_53_e": {
        "name": "7 Av/53 St (E)",
        "stop_ids": ("D14N", "D14S"),
        "routes": ("E",),
    },
    "57_7av": {
        "name": "57 St-7 Av",
        "stop_ids": ("R14N", "R14S"),
        "routes": ("N", "Q", "R", "W"),
    },
    "50_8av": {
        "name": "50 St (8 Av)",
        "stop_ids": ("A27N", "A27S"),
        "routes": ("C", "E"),
    },
    "50_bway": {
        "name": "50 St (Broadway-7 Av)",
        "stop_ids": ("120N", "120S"),
        "routes": ("1",),
    },
    "cc_59": {
        "name": "59 St-Columbus Circle",
        "stop_ids": ("A24N", "A24S", "127N", "127S"),
        "routes": ("A", "B", "C", "D", "1"),
    },
}
MTA_EXTRA_STATIONS = {
    "times_sq_42": {"name": "Times Sq-42 St", "stop_ids": ("R16N", "R16S", "127N", "127S", "A27N", "A27S"), "routes": ("1", "2", "3", "N", "Q", "R", "W", "A", "C", "E", "7")},
    "34_herald_sq": {"name": "34 St-Herald Sq", "stop_ids": ("D17N", "D17S", "R20N", "R20S"), "routes": ("B", "D", "F", "M", "N", "Q", "R", "W")},
    "lex_59": {"name": "Lexington Av/59 St", "stop_ids": ("R11N", "R11S", "635N", "635S"), "routes": ("4", "5", "6", "N", "R", "W")},
}
PENN_WIDGET = {
    "origin_stop_ids": ("D14N", "D14S"),
    "target_stop_ids": ("A32N", "A32S"),
    "route": "E",
    "direction": "S",
    "label": "Penn ETA from 7 Av/53 St",
}
MTA_SCALE_OPTIONS = ("1.0", "1.2", "1.4", "1.6", "1.8")

# RLock: /api/status holds the lock and calls get_view_url -> build_backyard_query (nested lock).
state_lock = threading.RLock()
nav_lock = threading.Lock()
mta_cache_lock = threading.Lock()
mta_cache = {"fetched_at": 0.0, "data": None, "error": ""}
state = {
    "current_view": "dakboard",
    "rotate": True,
    "durations": {"dakboard": 30, "camera": 30, "backyard": 30, "mta": 30},
    "backyard_layout": "list",
    "backyard_meta": ["relative"],
    "backyard_filter_class": "",
    "mta_extra_enabled": False,
    "mta_extra_station": "",
    "mta_scale": "1.4",
    "classifier_ignore_classes": ["bench"],
}


def parse_ignore_classes_input(raw):
    """Normalize ignore list from JSON list or comma-separated string."""
    if isinstance(raw, list):
        out = []
        for x in raw:
            s = str(x).strip().lower()
            if s:
                out.append(s)
        return sorted(set(out))
    if isinstance(raw, str):
        parts = [x.strip().lower() for x in raw.split(",") if x.strip()]
        return sorted(set(parts))
    return []


def push_classifier_settings_to_pc(ignore_list):
    """Sync ignore list to classifier PC (BACKYARD_BASE). Returns (ok, error_message)."""
    url = f"{BACKYARD_BASE.rstrip('/')}/api/classifier-settings"
    data = json.dumps({"ignore_classes": ignore_list}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except OSError:
            detail = str(e.code)
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, str(e)


def encode_backyard_query(layout, meta, filter_class=""):
    """URL query for backyard view; meta may be list or comma string (no lock)."""
    if isinstance(meta, str):
        meta = [x.strip() for x in meta.split(",") if x.strip()]
    if not isinstance(meta, list):
        meta = ["relative"]
    if not meta:
        meta = ["relative"]
    params = [("layout", str(layout)), ("meta", ",".join(str(x) for x in meta))]
    fc = (filter_class or "").strip().lower()
    if fc in BACKYARD_FILTER_CLASSES:
        params.append(("class", fc))
    return urllib.parse.urlencode(params)


def build_backyard_query():
    with state_lock:
        layout = state.get("backyard_layout", "list")
        meta = state.get("backyard_meta", ["relative"])
        fc = state.get("backyard_filter_class", "") or ""
    return encode_backyard_query(layout, meta, fc)


def _epoch_from_stop_time(stop_time):
    if not stop_time:
        return None
    if getattr(stop_time, "time", None):
        return int(stop_time.time)
    return None


def _direction_from_stop_id(stop_id):
    if isinstance(stop_id, str):
        if stop_id.endswith("N"):
            return "uptown"
        if stop_id.endswith("S"):
            return "downtown"
    return "unknown"


def _fetch_mta_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "rpi-kiosk/1.0", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(body)
    return feed


def _build_mta_payload():
    if gtfs_realtime_pb2 is None:
        return {"ok": False, "error": "missing dependency: gtfs-realtime-bindings"}
    now = int(time.time())
    all_events = []
    trip_rows = {}
    feed_warnings = []

    for feed_key, url in MTA_FEEDS.items():
        try:
            feed = _fetch_mta_feed(url)
        except DecodeError as e:
            feed_warnings.append(f"{feed_key}: decode error: {e}")
            continue
        except Exception as e:
            feed_warnings.append(f"{feed_key}: fetch error: {e}")
            continue
        for entity in feed.entity:
            if not entity.HasField("trip_update"):
                continue
            tu = entity.trip_update
            route = (tu.trip.route_id or "").strip().upper()
            if not route:
                continue
            trip_id = tu.trip.trip_id or entity.id or ""
            for stu in tu.stop_time_update:
                stop_id = (stu.stop_id or "").strip()
                if not stop_id:
                    continue
                eta = _epoch_from_stop_time(stu.arrival) or _epoch_from_stop_time(stu.departure)
                if not eta:
                    continue
                mins = max(0, int(math.ceil((eta - now) / 60.0)))
                if mins < 0 or mins > MTA_MAX_MINUTES:
                    continue
                all_events.append({
                    "trip_id": trip_id,
                    "route": route,
                    "stop_id": stop_id,
                    "direction": _direction_from_stop_id(stop_id),
                    "eta_epoch": eta,
                    "minutes": mins,
                })
                trip_rows.setdefault(trip_id, []).append({
                    "route": route,
                    "stop_id": stop_id,
                    "direction": _direction_from_stop_id(stop_id),
                    "eta_epoch": eta,
                })

    with state_lock:
        extra_enabled = bool(state.get("mta_extra_enabled", False))
        extra_station = state.get("mta_extra_station", "") or ""
        mta_scale = state.get("mta_scale", "1.4") or "1.4"

    stations = dict(MTA_STATIONS)
    if extra_enabled and extra_station in MTA_EXTRA_STATIONS:
        stations[f"extra:{extra_station}"] = MTA_EXTRA_STATIONS[extra_station]

    out_stations = []
    for station_key, station in stations.items():
        routes = set(station["routes"])
        stop_ids = set(station["stop_ids"])
        station_events = [e for e in all_events if e["route"] in routes and e["stop_id"] in stop_ids]
        station_events.sort(key=lambda e: e["eta_epoch"])
        # De-duplicate nearly-identical rows that can appear across direction/platform updates.
        seen = set()
        uniq_events = []
        for e in station_events:
            k = (e["route"], e["trip_id"], e["eta_epoch"])
            if k in seen:
                continue
            seen.add(k)
            uniq_events.append(e)
        arrivals = [{
            "route": e["route"],
            "minutes": e["minutes"],
            "eta_epoch": e["eta_epoch"],
            "direction": e.get("direction", "unknown"),
            "color": MTA_ROUTE_COLORS.get(e["route"], "#888888"),
        } for e in uniq_events[:10]]
        out_stations.append({
            "key": station_key,
            "name": station["name"],
            "arrivals": arrivals,
        })

    penn_candidates = []
    origin_ids = set(PENN_WIDGET["origin_stop_ids"])
    target_ids = set(PENN_WIDGET["target_stop_ids"])
    route = PENN_WIDGET["route"]
    direction = PENN_WIDGET.get("direction", "")
    for trip_rows_for_trip in trip_rows.values():
        r_events = [x for x in trip_rows_for_trip if x["route"] == route and (not direction or x.get("direction") == ("downtown" if direction == "S" else "uptown"))]
        if not r_events:
            continue
        origins = sorted([x for x in r_events if x["stop_id"] in origin_ids], key=lambda x: x["eta_epoch"])
        targets = sorted([x for x in r_events if x["stop_id"] in target_ids], key=lambda x: x["eta_epoch"])
        if not origins or not targets:
            continue
        origin = origins[0]
        target = next((t for t in targets if t["eta_epoch"] >= origin["eta_epoch"]), None)
        if not target:
            continue
        total_minutes = int((target["eta_epoch"] - now) / 60)
        wait_minutes = int((origin["eta_epoch"] - now) / 60)
        if total_minutes >= 0:
            penn_candidates.append({
                "total_minutes": total_minutes,
                "wait_minutes": max(0, wait_minutes),
                "origin_eta_epoch": origin["eta_epoch"],
            })
    penn_candidates.sort(key=lambda x: x["origin_eta_epoch"])
    best3 = penn_candidates[:3]
    penn = {
        "label": PENN_WIDGET["label"],
        "method": "best_of_next_3_e_trains",
        "available": bool(best3),
        "minutes": min((x["total_minutes"] for x in best3), default=None),
        "wait_minutes": min((x["wait_minutes"] for x in best3), default=None),
    }

    payload = {
        "ok": True,
        "generated_at": now,
        "stations": out_stations,
        "route_colors": MTA_ROUTE_COLORS,
        "penn_eta": penn,
        "extra_station_enabled": extra_enabled,
        "extra_station_key": extra_station if extra_enabled else "",
        "mta_scale": mta_scale if mta_scale in MTA_SCALE_OPTIONS else "1.4",
    }
    if feed_warnings:
        payload["warnings"] = feed_warnings
    if not all_events:
        payload["ok"] = False
        payload["error"] = "no parseable trip updates from MTA feeds"
    return payload


def get_mta_payload():
    now = time.time()
    with mta_cache_lock:
        cached = mta_cache.get("data")
        age = now - float(mta_cache.get("fetched_at", 0))
        if cached and age < MTA_CACHE_SECONDS:
            return cached
    try:
        payload = _build_mta_payload()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        payload = {"ok": False, "error": str(e)}
    except Exception as e:
        # Never let feed parsing/network edge cases crash the request thread.
        payload = {"ok": False, "error": f"mta feed parse failed: {e.__class__.__name__}: {e}"}
    with mta_cache_lock:
        mta_cache["fetched_at"] = now
        mta_cache["data"] = payload
        mta_cache["error"] = payload.get("error", "")
    return payload


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
            if "backyard_filter_class" in saved:
                bfc = saved["backyard_filter_class"]
                if isinstance(bfc, str) and (not bfc or bfc in BACKYARD_FILTER_CLASSES):
                    state["backyard_filter_class"] = bfc
            if "mta_extra_enabled" in saved:
                state["mta_extra_enabled"] = bool(saved["mta_extra_enabled"])
            if "mta_extra_station" in saved:
                key = saved["mta_extra_station"]
                if isinstance(key, str) and (not key or key in MTA_EXTRA_STATIONS):
                    state["mta_extra_station"] = key
            if "mta_scale" in saved:
                s = str(saved["mta_scale"])
                if s in MTA_SCALE_OPTIONS:
                    state["mta_scale"] = s
            if "classifier_ignore_classes" in saved:
                cic = saved["classifier_ignore_classes"]
                if isinstance(cic, list):
                    state["classifier_ignore_classes"] = parse_ignore_classes_input(cic)
                elif isinstance(cic, str):
                    state["classifier_ignore_classes"] = parse_ignore_classes_input(cic)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save_config():
    with state_lock:
        data = {
            "rotate": state["rotate"],
            "durations": dict(state["durations"]),
            "backyard_layout": state.get("backyard_layout", "list"),
            "backyard_meta": list(state.get("backyard_meta", ["relative"])),
            "backyard_filter_class": state.get("backyard_filter_class", "") or "",
            "mta_extra_enabled": bool(state.get("mta_extra_enabled", False)),
            "mta_extra_station": state.get("mta_extra_station", "") or "",
            "mta_scale": state.get("mta_scale", "1.4") if state.get("mta_scale", "1.4") in MTA_SCALE_OPTIONS else "1.4",
            "classifier_ignore_classes": list(state.get("classifier_ignore_classes", ["bench"])),
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
        sock = getattr(ws, "sock", None)
        if sock is not None:
            sock.settimeout(15)
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
.st.dakboard{color:#5dade2} .st.camera{color:#58d68d} .st.backyard{color:#bb8fce} .st.mta{color:#f5c242}
.r{display:flex;gap:.6rem;flex-wrap:wrap}
.b{flex:1;min-width:5rem;padding:.9rem;border:none;border-radius:.5rem;font-size:1rem;
   font-weight:600;cursor:pointer;background:var(--inp);color:#ccc;transition:.15s;
   display:flex;align-items:center;justify-content:center;text-align:center}
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
    <button class="b" id="bmta" onclick="sw('mta')">MTA</button>
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
  <div class="dr fb">
    <span>MTA</span>
    <div><input id="dmta" type="number" min="5" max="3600" value="30"
         onchange="sd('mta',this.value)"> s</div>
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
  <div class="meta" id="bf">
    <span style="width:100%;margin-bottom:.25rem">Show detections</span>
    <label><input type="checkbox" id="bf-all"> All types</label>
    <label><input type="checkbox" id="bf-bird" data-fc="bird"> Birds</label>
    <label><input type="checkbox" id="bf-cat" data-fc="cat"> Cats</label>
    <label><input type="checkbox" id="bf-dog" data-fc="dog"> Dogs</label>
    <label><input type="checkbox" id="bf-person" data-fc="person"> People</label>
  </div>
  <div class="dr" style="margin-top:.75rem">
    <span style="display:block;margin-bottom:.35rem">Ignore classes (no gallery save if only these)</span>
    <input type="text" id="cig" placeholder="bench, potted plant" style="width:100%;padding:.5rem;border:1px solid #333;border-radius:.4rem;background:var(--bg);color:#eaeaea;font-size:.9rem" autocomplete="off" spellcheck="false">
    <button type="button" class="b" style="margin-top:.5rem;width:100%" onclick="sci()">Apply to classifier</button>
    <p style="font-size:.72rem;color:#666;margin-top:.4rem;line-height:1.35">Comma-separated YOLO class names. Motion frames where <strong>every</strong> detection is ignored are not saved on the classifier PC.</p>
  </div>
</div>
<div class="c">
  <div class="lb">MTA options</div>
  <div class="meta">
    <label><input type="checkbox" id="me" onchange="sm()"> Show other station</label>
  </div>
  <div class="dr">
    <span style="display:block;margin-bottom:.35rem">Other station</span>
    <select id="ms" onchange="sm()">
      <option value="">-- none --</option>
      <option value="times_sq_42">Times Sq-42 St</option>
      <option value="34_herald_sq">34 St-Herald Sq</option>
      <option value="lex_59">Lexington Av/59 St</option>
    </select>
  </div>
  <div class="dr">
    <span style="display:block;margin-bottom:.35rem">Display scale</span>
    <select id="mz" onchange="sm()">
      <option value="1.0">100%</option>
      <option value="1.2">120%</option>
      <option value="1.4">140%</option>
      <option value="1.6">160%</option>
      <option value="1.8">180%</option>
    </select>
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
    $('dd').value=d.durations.dakboard; $('dc').value=d.durations.camera; $('db').value=d.durations.backyard; $('dmta').value=d.durations.mta||30;
    $('bd').className='b'+(d.current_view==='dakboard'?' on':'');
    $('bc').className='b'+(d.current_view==='camera'?' on':'');
    $('bb').className='b'+(d.current_view==='backyard'?' on':'');
    $('bmta').className='b'+(d.current_view==='mta'?' on':'');
    $('bl').value=d.backyard_layout||'list';
    const set=d.backyard_meta||['relative'];
    document.querySelectorAll('#bm input[data-m]').forEach(cb=>{
      cb.checked=set.indexOf(cb.dataset.m)>=0;
    });
    const fc=(d.backyard_filter_class||'').toLowerCase();
    $('bf-all').checked=!fc;
    document.querySelectorAll('#bf input[data-fc]').forEach(cb=>{
      cb.checked=cb.dataset.fc===fc;
    });
    const cic=d.classifier_ignore_classes||[];
    if(document.activeElement!==$('cig')){
      $('cig').value=Array.isArray(cic)?cic.join(', '):'';
    }
    $('me').checked=!!d.mta_extra_enabled;
    $('ms').value=d.mta_extra_station||'';
    $('ms').disabled=!$('me').checked;
    $('mz').value=d.mta_scale||'1.4';
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
  let filter_class='';
  if($('bf-all').checked)filter_class='';
  else document.querySelectorAll('#bf input[data-fc]').forEach(cb=>{
    if(cb.checked)filter_class=cb.dataset.fc;
  });
  handleApi(api('POST','backyard',{layout,meta,filter_class}),'Backyard');
}
function bfSync(ev){
  const t=ev.target;
  if(t.id==='bf-all'&&t.checked){
    document.querySelectorAll('#bf input[data-fc]').forEach(cb=>{cb.checked=false;});
  }else if(t.dataset&&t.dataset.fc&&t.checked){
    $('bf-all').checked=false;
    document.querySelectorAll('#bf input[data-fc]').forEach(cb=>{
      if(cb!==t)cb.checked=false;
    });
  }
  sb();
}
function sm(){
  const enabled=$('me').checked;
  $('ms').disabled=!enabled;
  handleApi(api('POST','mta-settings',{enabled:enabled,station_key:$('ms').value||'',scale:$('mz').value||'1.4'}),'MTA settings');
}
function sci(){
  api('POST','classifier-ignore',{ignore_classes:$('cig').value||''}).then(d=>{
    if(d&&d.ok===false){
      alert('Classifier ignore: '+(d.error||d.detail||'failed'));
    }else if(d&&d.classifier_sync_ok===false){
      alert('Saved on kiosk; classifier PC sync failed: '+(d.classifier_sync_error||'unknown')+'\n(Is the classifier running and reachable from the Pi?)');
    }
    rf();
  }).catch(()=>{alert('Classifier ignore failed (network)');rf();});
}
document.querySelectorAll('#bm input[data-m]').forEach(cb=>{
  cb.addEventListener('change',sb);
});
document.querySelectorAll('#bf input').forEach(cb=>{
  cb.addEventListener('change',bfSync);
});
rf(); setInterval(rf,3000);
</script></body></html>"""

MTA_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NYC Train Times</title>
<style>
:root{--bg:#0f1117;--card:#1b2333;--txt:#e8edf5;--muted:#a7b3c9}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--txt);padding:20px;line-height:1.25}
h1{font-size:44px;margin-bottom:16px}
.grid{display:flex;flex-direction:column;gap:14px}
.card{background:var(--card);border-radius:14px;padding:16px}
.name{font-size:30px;font-weight:700;margin-bottom:10px}
.arr{display:flex;flex-wrap:wrap;gap:10px}
.chip{display:flex;align-items:center;gap:10px;background:#0d1626;border-radius:999px;padding:8px 12px}
.bullet{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#111;font-weight:700;font-size:20px}
.mins{font-size:26px;font-weight:700}
.dir{font-size:18px;color:#c3d0e8;font-weight:600}
.penn{margin-bottom:14px;font-size:40px;font-weight:700}
.muted{color:var(--muted)}
</style></head><body>
<h1>NYC Subway Times</h1>
<div class="card" style="margin-bottom:12px">
  <div class="penn" id="penn">Penn ETA: --</div>
  <div class="muted" id="upd">Loading...</div>
</div>
<div class="grid" id="stations"></div>
<script>
function el(tag, cls, txt){const n=document.createElement(tag);if(cls)n.className=cls;if(txt!==undefined)n.textContent=txt;return n;}
function render(d){
  document.body.style.zoom = d.mta_scale || '1.4';
  const penn=d.penn_eta||{};
  document.getElementById('penn').textContent = penn.available ? ('Penn ETA: '+penn.minutes+' min') : 'Penn ETA: --';
  document.getElementById('upd').textContent='Updated '+new Date((d.generated_at||0)*1000).toLocaleTimeString();
  const root=document.getElementById('stations'); root.innerHTML='';
  (d.stations||[]).forEach(s=>{
    const card=el('div','card');
    card.appendChild(el('div','name',s.name));
    const row=el('div','arr');
    if(!s.arrivals||!s.arrivals.length){row.appendChild(el('div','muted','No imminent arrivals'));} else {
      s.arrivals.slice(0,6).forEach(a=>{
        const chip=el('div','chip');
        const b=el('div','bullet',a.route); b.style.background=(a.color||'#888');
        chip.appendChild(b);
        chip.appendChild(el('div','mins',a.minutes+' min'));
        const dir=a.direction==='uptown'?'Uptown ↑':(a.direction==='downtown'?'Downtown ↓':'?');
        chip.appendChild(el('div','dir',dir));
        row.appendChild(chip);
      });
    }
    card.appendChild(row);
    root.appendChild(card);
  });
}
function tick(){
  fetch('/api/mta-arrivals').then(r=>r.json()).then(d=>{
    if(d&&d.ok) render(d);
    else document.getElementById('upd').textContent='Feed unavailable';
  }).catch(()=>{document.getElementById('upd').textContent='Network error';});
}
tick(); setInterval(tick,15000);
</script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _path(self):
        """Path without query string (Chromium may send ?…)."""
        return self.path.split("?", 1)[0]

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self):
        p = self._path()
        if p == "/":
            body = CONTROL_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/mta":
            body = MTA_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/ping":
            self._json({"ok": True, "service": "kiosk-controller"})
        elif p == "/api/mta-arrivals":
            self._json(get_mta_payload())
        elif p == "/api/status":
            try:
                with state_lock:
                    cv = state["current_view"]
                    layout = state.get("backyard_layout", "list")
                    meta = list(state.get("backyard_meta", ["relative"]))
                    bfc = state.get("backyard_filter_class", "") or ""
                    mta_extra_enabled = bool(state.get("mta_extra_enabled", False))
                    mta_extra_station = state.get("mta_extra_station", "") or ""
                    mta_scale = state.get("mta_scale", "1.4")
                    d = {
                        "current_view": cv,
                        "current_view_name": VIEWS.get(cv, {}).get("name", cv),
                        "rotate": state["rotate"],
                        "durations": dict(state["durations"]),
                        "backyard_layout": layout,
                        "backyard_meta": meta,
                        "backyard_filter_class": bfc,
                        "mta_extra_enabled": mta_extra_enabled,
                        "mta_extra_station": mta_extra_station,
                        "mta_scale": mta_scale if mta_scale in MTA_SCALE_OPTIONS else "1.4",
                        "classifier_ignore_classes": list(
                            state.get("classifier_ignore_classes", ["bench"])
                        ),
                    }
                if "backyard" in VIEWS:
                    d["backyard_url"] = f"{BACKYARD_BASE}/?{encode_backyard_query(layout, meta, bfc)}"
                else:
                    d["backyard_url"] = ""
                self._json(d)
            except Exception as e:
                print(f"kiosk-controller: /api/status error: {e}", file=sys.stderr)
                self._json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_error(404)

    def do_POST(self):
        p = self._path()
        if p == "/api/switch":
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

        elif p == "/api/rotate":
            with state_lock:
                state["rotate"] = not state["rotate"]
                r = state["rotate"]
            save_config()
            self._json({"ok": True, "rotate": r})

        elif p == "/api/duration":
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

        elif p == "/api/backyard":
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
            fc = b.get("filter_class", b.get("class", ""))
            if not isinstance(fc, str):
                fc = ""
            fc = fc.strip().lower()
            if fc and fc not in BACKYARD_FILTER_CLASSES:
                self._json({"ok": False, "error": "invalid filter_class"}, 400)
                return
            with state_lock:
                state["backyard_layout"] = layout
                state["backyard_meta"] = clean
                state["backyard_filter_class"] = fc
            save_config()
            with state_lock:
                on_backyard = state.get("current_view") == "backyard"
            if on_backyard:
                ok, err = switch_to("backyard")
                if not ok:
                    self._json({"ok": False, "error": "navigate failed", "detail": err or ""}, 400)
                    return
            self._json({"ok": True})
        elif p == "/api/mta-settings":
            b = self._body()
            enabled = bool(b.get("enabled", False))
            station_key = b.get("station_key", "")
            scale = str(b.get("scale", "1.4"))
            if not isinstance(station_key, str):
                station_key = ""
            station_key = station_key.strip()
            if station_key and station_key not in MTA_EXTRA_STATIONS:
                self._json({"ok": False, "error": "invalid station_key"}, 400)
                return
            if scale not in MTA_SCALE_OPTIONS:
                self._json({"ok": False, "error": "invalid scale"}, 400)
                return
            with state_lock:
                state["mta_extra_enabled"] = enabled
                state["mta_extra_station"] = station_key
                state["mta_scale"] = scale
            save_config()
            with mta_cache_lock:
                mta_cache["fetched_at"] = 0.0
                mta_cache["data"] = None
                mta_cache["error"] = ""
            self._json({"ok": True})
        elif p == "/api/classifier-ignore":
            b = self._body()
            raw = b.get("ignore_classes", [])
            lst = parse_ignore_classes_input(raw)
            with state_lock:
                state["classifier_ignore_classes"] = lst
            save_config()
            ok_pc, err_pc = push_classifier_settings_to_pc(lst)
            self._json({
                "ok": True,
                "classifier_ignore_classes": lst,
                "classifier_sync_ok": ok_pc,
                "classifier_sync_error": err_pc,
            })
        else:
            self.send_error(404)


def main():
    load_config()
    with state_lock:
        _ign = list(state.get("classifier_ignore_classes", ["bench"]))
    _ok, _err = push_classifier_settings_to_pc(_ign)
    if not _ok:
        print(
            f"kiosk-controller: classifier settings sync failed (PC may be off): {_err}",
            file=sys.stderr,
        )
    threading.Thread(target=rotation_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    print(f"Kiosk controller on :{CONTROL_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()