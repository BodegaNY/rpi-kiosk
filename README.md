# RPi Kiosk & Camera System

Raspberry Pi hallway kiosk that rotates between [DAKboard](https://dakboard.com) and a live camera feed, with a web-based control panel.

## Devices

| Device | Hardware | Hostname | User | Tailscale IP | LAN IP |
|--------|----------|----------|------|-------------|--------|
| Camera | Pi Zero W (armv6) | `rpi-cam1` | `pi` | `100.66.35.101` | `192.168.86.33` |
| Kiosk | Pi 3 B+ (aarch64) | `rpi3b-hallway-kiosk` | `rpi3b` | `100.93.242.68` | `192.168.86.30` |

Both run Pi OS Trixie (Debian trixie) and are connected via Tailscale.

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
2. **kiosk-controller.py** (systemd user service) connects to Chromium via CDP WebSocket and rotates between Dakboard and the camera viewer every 30 seconds (configurable)
3. **cam-viewer.html** is a local HTML wrapper that displays the MJPEG stream with auto-reconnect on disconnect
4. **Control panel** served on port 8088 — switch views, toggle rotation, adjust durations

### Control panel

- LAN: http://192.168.86.30:8088
- Tailscale: http://100.93.242.68:8088

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
sudo cp /tmp/kiosk-controller.py /usr/local/bin/kiosk-controller.py && sudo chmod +x /usr/local/bin/kiosk-controller.py
cp /tmp/autostart ~/.config/labwc/autostart
cp /tmp/cam-viewer.html ~/cam-viewer.html
cp /tmp/kiosk-controller.service ~/.config/systemd/user/kiosk-controller.service
systemctl --user daemon-reload && systemctl --user restart kiosk-controller.service
# Or just: sudo reboot
```

## DAKboard login

DAKboard requires a one-time browser login. Connect a keyboard/mouse to the kiosk Pi, log in when the Dakboard page appears, then disconnect. The session cookie persists in `~/.config/chromium/` across reboots.
