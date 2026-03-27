# RPi Kiosk & Camera System

Raspberry Pi hallway kiosk that rotates between [DAKboard](https://dakboard.com), a live camera feed, the **Backyard** detection gallery (classifier PC over Tailscale), and an **NYC MTA arrivals** page, with a web-based control panel.

## Devices

| Device | Hardware | Hostname | User | Tailscale IP | LAN IP |
|--------|----------|----------|------|-------------|--------|
| Camera | Pi Zero W (armv6) | `rpi-cam1` | `pi` | `100.66.35.101` | `192.168.86.33` |
| Kiosk | Pi 3 B+ (aarch64) | `rpi3b-hallway-kiosk` | `rpi3b` | `100.93.242.68` | `192.168.86.30` |
| Classifier PC | Windows (RTX 4070) | `jasonbequiet` | — | `100.123.231.73` | `192.168.86.28` |

Both run Pi OS Trixie (Debian trixie) and are connected via Tailscale.

**Motion → classifier:** Use the classifier PC’s **Tailscale** URL (`http://100.123.231.73:8089/...`) from `rpi-cam1`. LAN (`192.168.86.28`) can return *connection refused* from the Pi Zero if Wi‑Fi client isolation or routing differs; Tailscale avoids that.

## Camera Pi — rpi-cam1

**`cam-mjpeg-http.py`** → deployed to `/usr/local/bin/cam-mjpeg-http.py`

- Multi-client MJPEG-over-HTTP server on port 8080
- Single `rpicam-vid` process broadcasts frames to all connected viewers
- IMX219 (Camera Module 2), 1280×720 @ 10 fps, JPEG quality 75
- systemd unit: `cam-stream.service`
- Stream URL: `http://<ip>:8080/cam.mjpg`

## Kiosk Pi — rpi3b-hallway-kiosk

### Files and deployment paths

| Local file | Deployed to |
|-----------|------------|
| `autostart` | `~/.config/labwc/autostart` |
| `kiosk-controller.py` | `/usr/local/bin/kiosk-controller.py` |
| `kiosk-controller.service` | `~/.config/systemd/user/kiosk-controller.service` |
| `cam-viewer.html` | `~/cam-viewer.html` |

### How it works

1. **labwc autostart** waits for network/Tailscale, clears Chromium crash state, launches Chromium in kiosk mode with CDP remote debugging on port 9222
2. **kiosk-controller.py** (systemd user service) connects to Chromium via CDP WebSocket and rotates between Dakboard, the camera viewer, the Backyard gallery (Tailscale URL, default `http://100.123.231.73:8089`), and a local MTA page (`http://127.0.0.1:8088/mta`) on configurable dwell times
3. **cam-viewer.html** is a local HTML wrapper that displays the MJPEG stream with auto-reconnect on disconnect
4. **Control panel** served on port 8088 — switch views, toggle rotation, adjust per-view durations, set Backyard gallery layout/metadata/object filters, and configure optional extra MTA station display (stored in `~/.kiosk-config.json` on the Pi)

### Control panel

- LAN: http://192.168.86.30:8088
- Tailscale: http://100.93.242.68:8088

**HTTP API (JSON):** `GET /api/status` (includes `durations`, `backyard_layout`, `backyard_meta`, `backyard_url`, `mta_extra_enabled`, `mta_extra_station`, `mta_scale`), `GET /api/mta-arrivals`, `POST /api/switch` body `{"view":"dakboard"|"camera"|"backyard"|"mta"}`, `POST /api/rotate`, `POST /api/duration` body `{"view":"...","seconds":30}`, `POST /api/backyard` body `{"layout":"list"|"highlight_recent","meta":["relative","model",...],"filter_class":"bird|cat|dog|person|\"\""}`, `POST /api/mta-settings` body `{"enabled":true|false,"station_key":"times_sq_42|34_herald_sq|lex_59|\"\"","scale":"1.0|1.2|1.4|1.6|1.8"}`.

### Kiosk workarounds

- `--password-store=basic` bypasses gnome-keyring unlock prompt
- gnome-keyring autostart disabled via `~/.config/autostart/gnome-keyring-*.desktop` (Hidden=true)
- Chromium crash-restore suppressed via `sed` in autostart + `--hide-crash-restore-bubble` + `--disable-features=InfiniteSessionRestore`

## Deploying changes

### To the camera Pi (via Tailscale)

```bash
scp cam-mjpeg-http.py pi@100.66.35.101:/tmp/cam-mjpeg-http.py
ssh pi@100.66.35.101 "sudo cp /tmp/cam-mjpeg-http.py /usr/local/bin/cam-mjpeg-http.py && sudo chmod +x /usr/local/bin/cam-mjpeg-http.py && sudo systemctl restart cam-stream.service"
```

### To the kiosk Pi (via LAN)

```bash
scp kiosk-controller.py rpi3b@192.168.86.30:/tmp/kiosk-controller.py
scp autostart rpi3b@192.168.86.30:/tmp/autostart
scp cam-viewer.html rpi3b@192.168.86.30:/tmp/cam-viewer.html
scp kiosk-controller.service rpi3b@192.168.86.30:/tmp/kiosk-controller.service

ssh rpi3b@192.168.86.30
python3 -m pip install --user gtfs-realtime-bindings
sudo cp /tmp/kiosk-controller.py /usr/local/bin/kiosk-controller.py && sudo chmod +x /usr/local/bin/kiosk-controller.py
cp /tmp/autostart ~/.config/labwc/autostart
cp /tmp/cam-viewer.html ~/cam-viewer.html
cp /tmp/kiosk-controller.service ~/.config/systemd/user/kiosk-controller.service
systemctl --user daemon-reload && systemctl --user restart kiosk-controller.service
# Or just: sudo reboot
```

**Control panel (port 8088):** The web UI is part of `kiosk-controller.py` on the Pi — it does **not** update when you change code on Windows. You must **scp** the new script to the Pi and **restart** `kiosk-controller` (or reboot) so button taps actually drive Chromium via CDP. If the page loads but **View** buttons do nothing, redeploy: recent versions fix Chromium’s `/json` list so we attach to a **`page`** target, not the **`browser`** target (which ignores `Page.navigate`).

**If `curl` to `/api/status` hangs or times out:** Older builds could **deadlock** while handling status (nested lock + `get_view_url` / backyard query). Current `main` avoids that, adds **`GET /api/ping`** (no locks), normalizes paths, and uses **`RLock`** / `encode_backyard_query` as needed. On the Pi after deploy: `curl -sS http://127.0.0.1:8088/api/ping` then `curl -sS http://127.0.0.1:8088/api/status`.

## Motion detection + classifier (rpi-cam1 → Windows)

- **`motion-detect.py`** → `/usr/local/bin/motion-detect.py`, systemd: `motion-detect.service`
- Config: `/home/pi/.motion-config.json` — use **`classifier_url`** with the classifier PC’s **Tailscale** host (see below). WoL MAC / broadcast are optional if the PC is usually on.
- **`classifier-server/`** — FastAPI + YOLOv8 nano on port **8089**. Detections saved under `classifier-server/detections/` (gitignored).

### URLs (jasonbequiet / classifier PC)

| Use | URL |
|-----|-----|
| Pi → classify | `http://100.123.231.73:8089/api/classify` |
| Health check | `http://100.123.231.73:8089/health` |
| Gallery (Tailscale) | `http://100.123.231.73:8089` |
| Gallery (on PC only) | `http://localhost:8089` |
| List detections (JSON) | `GET /api/detections` (optional `?class=bird` etc.) |
| Delete one detection | `DELETE /api/detections/{id}` (removes folder under `detections/`) |

**Gallery URL query string** (read on load; kiosk uses these when opening Backyard):

- `layout=list` or `layout=highlight_recent` (hero + 3-column strip below)
- `meta=relative,iso,conf,bbox,model,size` (comma-separated; omit flags you do not want)

Example: `http://100.123.231.73:8089/?layout=highlight_recent&meta=relative,model,size`

**Why Tailscale from the camera Pi:** LAN `192.168.86.28` may **connection refused** from `rpi-cam1` (Wi‑Fi client isolation or routing). Tailscale from `100.66.35.101` → `100.123.231.73` works reliably.

### Run classifier on Windows (this repo)

From the repo’s `classifier-server` folder (or clone the repo to e.g. `C:\data\classifier-server` and `cd` into `classifier-server`):

```powershell
pip install -r requirements.txt
python server.py
```

Leave the process running (or use Task Scheduler). First run downloads `yolov8n.pt` (~6 MB) next to `server.py`.

### Windows Firewall (admin PowerShell)

If other devices cannot reach port 8089, add:

```powershell
New-NetFirewallRule -DisplayName "Classifier Server TCP 8089" -Direction Inbound -LocalPort 8089 -Protocol TCP -Action Allow -Profile Any
```

For **Microsoft Store Python**, also allow the real executable (path may change after a Python update — confirm with `Get-Process python3.12 | Select-Object Path` while `server.py` is running):

```powershell
New-NetFirewallRule -DisplayName "Python 3.12 Classifier" -Direction Inbound -Program "C:\Program Files\WindowsApps\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0\python3.12.exe" -Action Allow -Profile Any
```

### Deploy motion detector to rpi-cam1

```bash
scp motion-detect.py motion-detect.service pi@100.66.35.101:/tmp/
ssh pi@100.66.35.101
sudo apt install -y python3-numpy python3-pil
sudo cp /tmp/motion-detect.py /usr/local/bin/motion-detect.py && sudo chmod +x /usr/local/bin/motion-detect.py
sudo cp /tmp/motion-detect.service /etc/systemd/system/motion-detect.service
mkdir -p ~/motion-captures
sudo systemctl daemon-reload
sudo systemctl enable --now motion-detect.service
```

### Point rpi-cam1 at classifier (Tailscale) and restart

```bash
sudo systemctl stop motion-detect.service
sed -i 's|http://[^"]*8089/api/classify|http://100.123.231.73:8089/api/classify|' /home/pi/.motion-config.json
grep classifier_url /home/pi/.motion-config.json
sudo systemctl start motion-detect.service
```

Verify from the Pi:

```bash
curl -s http://100.123.231.73:8089/health
```

## DAKboard login

DAKboard requires a one-time browser login. Connect a keyboard/mouse to the kiosk Pi, log in when the Dakboard page appears, then disconnect. The session cookie persists in `~/.config/chromium/` across reboots.
