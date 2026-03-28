# Agent handoff — `rpi-kiosk`

GitHub: **BodegaNY/rpi-kiosk** (`main`). Source of truth for Pi + Windows classifier in this folder.

## What this project is

- **Kiosk Pi** (`rpi3b-hallway-kiosk`): Chromium kiosk + `kiosk-controller.py` on **:8088** (CDP :9222). Rotates views and serves the phone control panel.
- **Camera Pi** (`rpi-cam1`): MJPEG + `motion-detect.py` → POSTs JPEGs to the classifier.
- **Classifier PC** (Windows): `classifier-server/server.py` on **:8089** (YOLOv8, FastAPI gallery).

Tailscale is used between devices; IPs in code/README are examples — confirm live IPs in `kiosk-controller.py` (`BACKYARD_BASE`) and motion config.

## Views (kiosk rotation)

| Key        | Description |
|-----------|-------------|
| `dakboard`| External Dakboard URL |
| `camera`  | `file:///home/rpi3b/cam-viewer.html` |
| `backyard`| `BACKYARD_BASE` + query (`layout`, `meta`, `class`) |
| `mta`     | `http://127.0.0.1:8088/mta` — NYC subway board (GTFS-RT on Pi) |

Config persisted: `~/.kiosk-config.json` on the Pi (durations, rotate, backyard prefs, MTA prefs, **`classifier_ignore_classes`**).

## Kiosk HTTP API (port 8088)

- `GET /api/status`, `GET /api/ping`, `POST /api/switch`, `POST /api/rotate`, `POST /api/duration`
- `POST /api/backyard` — layout, meta flags, `filter_class` (gallery animal filter)
- `POST /api/mta-settings` — extra station toggle, `scale` (1.0–1.8)
- `GET /api/mta-arrivals`, `GET /mta`
- `POST /api/classifier-ignore` — body `ignore_classes` (string or list); saves on Pi and **POSTs** to `BACKYARD_BASE/api/classifier-settings`

On startup, controller tries to push ignore list to the classifier (stderr warning if PC unreachable).

## MTA implementation (`kiosk-controller.py`)

- Fetches MTA protobuf feeds from `api-endpoint.mta.info` (paths `nyct/gtfs`, `gtfs-ace`, `gtfs-bdfm`, `gtfs-nqrw`); responses may be **gzip** — decompress before parse.
- **Pi dependency:** `gtfs-realtime-bindings` (install with `pip install --user --break-system-packages` on Pi OS if PEP 668 blocks).
- Cached ~20s; per-feed errors become `warnings` in JSON when other feeds succeed.
- Direction: **Uptown/Downtown** from stop id suffix `N`/`S`.
- Penn ETA: E train, best-of-next-3, downtown-only, 7 Av/53 St → Penn.

## Classifier (`classifier-server/server.py`)

- `POST /api/classify` — raw JPEG body.
- **`classifier-settings.json`** (gitignored): `ignore_classes` (lowercase YOLO names). `GET/POST /api/classifier-settings`.
- If **every** detection is ignored → **no** folder under `detections/`; response includes `saved: false`, `ignored_only: true`.
- `GET /api/detections` — omits rows that are **only** ignored classes after filtering; strips ignored classes from labels on mixed rows.

## Deploy reminders

- **Pi kiosk:** `scp kiosk-controller.py` → `/usr/local/bin/`, `systemctl --user restart kiosk-controller`. If port 8088 stuck: `pkill -f kiosk-controller.py` then restart.
- **Windows classifier:** `netstat -ano | findstr :8089` → `taskkill /PID … /F` → `cd classifier-server` → `python .\server.py`. Use **`curl.exe`** not `curl` in PowerShell for URLs.
- Control panel HTML is **embedded** in `kiosk-controller.py` — redeploy script for UI changes.
- Ignore-classes field: `rf()` skips overwriting `#cig` while it has focus (comma typing fix).

## Docs in repo

- `README.md` — deploy, APIs, URLs.
- `docs/assessment.md` — security/reliability review (older).
- `docs/mta-assessment.md` — MTA feature notes.
- `.cursor/rules/rpi-kiosk-context.mdc` — Cursor rule pointer (same stack summary).

## Not built yet (discussed)

- Discord/webhook (or other) alerts on animal detection — hook after classify with cooldown would go in `server.py`.

## Workspace layout note

If the Cursor workspace root is `d:\Projects\scripts` (parent folder), the **git repo** for this stack is **`rpi-kiosk/`** only. Commit and push from `d:\Projects\scripts\rpi-kiosk`.
