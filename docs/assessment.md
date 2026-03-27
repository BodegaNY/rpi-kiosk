# Code Assessment — rpi-kiosk Project

**Date:** 2026-03-26
**Reviewed files:** `cam-mjpeg-http.py`, `cam-viewer.html`, `motion-detect.py`, `kiosk-controller.py`, `classifier-server/server.py`

---

## Architecture Overview

```
rpi-cam1 (Pi Zero W)
  cam-mjpeg-http.py  → :8080/cam.mjpg  (MJPEG stream, multi-client)
  motion-detect.py   → POST 100.123.231.73:8089/api/classify

jasonbequiet (Windows PC, RTX 4070)
  classifier-server/server.py  → :8089 (FastAPI + YOLOv8 nano)

rpi3b-hallway-kiosk (Pi 3B+)
  kiosk-controller.py  → :8088 (web control panel)
  Chromium kiosk ← CDP :9222

iPhone (controller)
  Browser → http://rpi3b-hallway-kiosk:8088
```

All devices connected via Tailscale VPN.

### Tailscale IPs

| Device | Tailscale IP | Role |
|--------|-------------|------|
| rpi-cam1 | 100.66.35.101 | Camera stream + motion detection |
| rpi3b-hallway-kiosk | 100.93.242.68 | Kiosk display + control |
| jasonbequiet (PC) | 100.123.231.73 | YOLOv8 classifier |
| iphone182 | 100.96.77.77 | Phone controller |

---

## Overall Verdict

Well-structured and clean. The separation of concerns is good — camera streaming, motion detection, classification, and display are independent services communicating over Tailscale. Thread safety is handled correctly throughout. A couple of real bugs need fixing before this is production-solid.

---

## Bugs That Need Fixing

### 1. Path traversal in `serve_detection_image` — `classifier-server/server.py:159-164`

**Severity: Medium (security)**

The `DELETE /api/detections/{id}` endpoint validates for `..` in the ID, but the image-serving endpoint does not:

```python
@app.get("/detections/{detection_id}/{filename}")
async def serve_detection_image(detection_id: str, filename: str):
    filepath = DETECTIONS_DIR / detection_id / filename  # no traversal check
    if not filepath.exists() or not filepath.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(filepath, media_type="image/jpeg")
```

A request like `GET /detections/../server.py/x` sets `detection_id=".."` and the resolved path escapes `DETECTIONS_DIR`. Fix: resolve the path and assert it starts with `DETECTIONS_DIR.resolve()`, matching the same logic already in `delete_detection`.

---

### 2. Unhandled exception in `_body()` — `kiosk-controller.py:372-374`

**Severity: Low (reliability)**

```python
def _body(self):
    n = int(self.headers.get("Content-Length", 0))
    return json.loads(self.rfile.read(n)) if n else {}
```

If `Content-Length` is missing/non-integer, or the body is invalid JSON, this throws an uncaught exception out of `do_POST`. `ThreadingHTTPServer` doesn't catch it gracefully — the client gets a broken connection. Should be wrapped in try-except returning a 400 response.

---

### 3. `upload_queue` maxlen ignores config — `motion-detect.py:38`

**Severity: Low (silent misconfiguration)**

```python
upload_queue = deque(maxlen=DEFAULTS["max_queue"])  # module-level, before load_config()
```

`load_config()` runs later in `main()`. If the config file has a different `max_queue`, the deque's capacity is already fixed — `deque.maxlen` is immutable after creation. Config changes to `max_queue` are silently ignored at runtime.

---

## Non-Bug Issues

### Hardcoded Tailscale IPs

Three files contain hardcoded IPs that must be updated manually if Tailscale addresses change:

| File | Line | Hardcoded value |
|------|------|-----------------|
| `cam-viewer.html` | 6 | `100.66.35.101` (camera Pi) |
| `kiosk-controller.py` | 18 | `100.123.231.73` (classifier PC) |
| `motion-detect.py` | 25 | `100.123.231.73` (classifier PC) |

Consider: environment variables, a shared config file, or Tailscale MagicDNS hostnames (e.g. `jasonbequiet.tail...ts.net`).

### No authentication on control panel (`:8088`)

Anyone on the Tailscale network can switch views, toggle rotation, or change settings. Low risk for a personal home network, but worth noting if the Tailscale ACL ever broadens.

### `cleanup_old_captures` silently skips subdirectories — `motion-detect.py:133-136`

```python
for f in CAPTURE_DIR.iterdir():
    if f.stat().st_mtime < cutoff.timestamp():
        f.unlink(missing_ok=True)  # raises IsADirectoryError on dirs
```

`f.unlink()` on a directory raises `IsADirectoryError`, which is caught silently by the outer `except OSError`. Any subdirectory older than `cleanup_days` would never be removed. In practice there shouldn't be any subdirectories, but it's a silent no-op if there are.

---

## What Works Well

| Component | Notes |
|-----------|-------|
| `cam-mjpeg-http.py` | Clean one-process/many-client fan-out via `FrameBroadcaster`. Proper subprocess lifecycle (terminate → wait → kill). |
| `motion-detect.py` | Solid offline queue with WoL wake + exponential retry. Frame diffing at 320×240 is efficient. |
| `kiosk-controller.py` | Correct `RLock` use for nested state access, separate `nav_lock` for CDP serialization, config saved on every mutation. |
| `server.py` | Clean FastAPI layout. DELETE path traversal protection is correct. Label generation and detection enrichment are well-structured. |
| Thread safety | All shared state protected by locks. `RLock` used appropriately where nested acquisition occurs. |
| Error recovery | All services recover from crashes/disconnects (subprocess restart, stream reconnect, CDP polling). |

---

## Component-by-Component Notes

### `cam-mjpeg-http.py`

- MJPEG parts omit `Content-Length` header, which is valid for streaming but some strict parsers may prefer it.
- The `_event.set(); _event.clear()` pattern means subscribers rely on the 2-second `wait(timeout=2)` fallback to pick up frames if they miss the pulse. At 10 fps sampling every 10th frame, this is harmless in practice.

### `motion-detect.py`

- `STREAM_URL` is hardcoded to `localhost:8080` — correct, since motion detect runs on the same Pi as the camera stream.
- WoL uses LAN broadcast (`192.168.86.255`), not Tailscale. If the PC is asleep, it won't have a Tailscale address, so LAN broadcast is correct here.
- No feedback on whether WoL succeeded — motion detect just queues and retries, which is the right approach.

### `kiosk-controller.py`

- CDP `cdp_navigate` can block up to ~25 seconds (10s WS connect + 15s socket timeout) if Chromium hangs. HTTP handler threads are independent so the control panel stays responsive during a stuck navigation.
- The rotation loop correctly breaks early when `rotate` is toggled off or the view is manually changed mid-sleep.

### `classifier-server/server.py`

- `get_model()` lazy-loads YOLOv8 on first classify request. `__main__` block calls it eagerly so the model is warm before the first request — good.
- Gallery auto-refreshes every 10 seconds (`setInterval(load, 10000)`). Over long kiosk sessions this is a minor continuous load; acceptable for home use.
- No CORS headers. Not needed currently since all access is same-origin or from the kiosk (which opens the URL directly), but worth knowing if the gallery is ever embedded elsewhere.
