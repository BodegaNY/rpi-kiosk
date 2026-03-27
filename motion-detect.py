#!/usr/bin/env python3
"""Motion detector: frame-diffs the local MJPEG stream, POSTs captures to a classifier."""

import io
import json
import os
import shutil
import socket
import struct
import time
import threading
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from PIL import Image

CONFIG_PATH = "/home/pi/.motion-config.json"
CAPTURE_DIR = Path("/home/pi/motion-captures")
STREAM_URL = "http://localhost:8080/cam.mjpg"

DEFAULTS = {
    "classifier_url": "http://100.123.231.73:8089/api/classify",
    "wol_mac": "D8:43:AE:81:20:84",
    "wol_broadcast": "192.168.86.255",
    "pixel_threshold": 30,
    "area_percent": 2.0,
    "cooldown_sec": 10,
    "diff_interval_frames": 10,
    "retry_delay_sec": 30,
    "max_queue": 50,
    "cleanup_days": 7,
}

config = dict(DEFAULTS)
upload_queue = deque(maxlen=DEFAULTS["max_queue"])
queue_lock = threading.Lock()


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            saved = json.load(f)
        config.update({k: saved[k] for k in saved if k in DEFAULTS})
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass


def save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except OSError:
        pass


def send_wol(mac_str, broadcast="255.255.255.255"):
    mac_bytes = bytes.fromhex(mac_str.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, (broadcast, 9))


def post_image(filepath):
    """POST a JPEG to the classifier. Returns JSON response or None on failure."""
    data = filepath.read_bytes()
    req = urllib.request.Request(
        config["classifier_url"],
        data=data,
        headers={"Content-Type": "image/jpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def classifier_reachable():
    """Quick check if the classifier HTTP port is open."""
    try:
        url = config["classifier_url"].rsplit("/", 1)[0] + "/health"
        urllib.request.urlopen(url, timeout=3)
        return True
    except Exception:
        return False


def upload_worker():
    """Background thread that retries queued images and sends WoL if needed."""
    wol_sent_at = 0
    while True:
        time.sleep(5)
        with queue_lock:
            if not upload_queue:
                continue
            filepath = upload_queue[0]

        result = post_image(filepath)
        if result is not None:
            with queue_lock:
                if upload_queue and upload_queue[0] == filepath:
                    upload_queue.popleft()
            save_detection_result(filepath, result)
        else:
            now = time.monotonic()
            if now - wol_sent_at > config["retry_delay_sec"]:
                try:
                    send_wol(config["wol_mac"], config["wol_broadcast"])
                except Exception:
                    pass
                wol_sent_at = now
            time.sleep(config["retry_delay_sec"])


def save_detection_result(filepath, result):
    """Save classifier response alongside the capture."""
    meta_path = filepath.with_suffix(".json")
    try:
        meta_path.write_text(json.dumps(result, indent=2))
    except OSError:
        pass


def cleanup_old_captures():
    """Delete captures older than cleanup_days."""
    cutoff = datetime.now() - timedelta(days=config["cleanup_days"])
    try:
        for f in CAPTURE_DIR.iterdir():
            if f.stat().st_mtime < cutoff.timestamp():
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    f.unlink(missing_ok=True)
    except OSError:
        pass


def extract_jpegs(stream):
    """Yield JPEG frames from an MJPEG HTTP stream."""
    buf = b""
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        buf += chunk
        while True:
            s = buf.find(b"\xff\xd8")
            if s < 0:
                buf = buf[-1:] if buf else b""
                break
            e = buf.find(b"\xff\xd9", s + 2)
            if e < 0:
                buf = buf[s:]
                break
            yield buf[s : e + 2]
            buf = buf[e + 2 :]


def frame_to_gray_small(jpeg_bytes):
    """Decode JPEG, convert to grayscale, downscale to 320x240 for diffing."""
    img = Image.open(io.BytesIO(jpeg_bytes))
    img = img.convert("L").resize((320, 240), Image.NEAREST)
    return np.frombuffer(img.tobytes(), dtype=np.uint8).reshape(240, 320)


def detect_motion(prev, curr):
    """Return True if motion exceeds configured thresholds."""
    diff = np.abs(curr.astype(np.int16) - prev.astype(np.int16))
    changed = np.count_nonzero(diff > config["pixel_threshold"])
    total = curr.size
    percent = (changed / total) * 100
    return percent > config["area_percent"]


def main():
    global upload_queue
    load_config()
    upload_queue = deque(maxlen=config["max_queue"])
    save_config()
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(target=upload_worker, daemon=True).start()

    last_cleanup = time.monotonic()
    last_trigger = 0
    prev_gray = None
    frame_count = 0

    while True:
        try:
            req = urllib.request.Request(STREAM_URL)
            with urllib.request.urlopen(req, timeout=10) as stream:
                for jpeg_bytes in extract_jpegs(stream):
                    frame_count += 1

                    if frame_count % config["diff_interval_frames"] != 0:
                        continue

                    curr_gray = frame_to_gray_small(jpeg_bytes)

                    if prev_gray is None:
                        prev_gray = curr_gray
                        continue

                    now = time.monotonic()

                    if now - last_trigger >= config["cooldown_sec"]:
                        if detect_motion(prev_gray, curr_gray):
                            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filepath = CAPTURE_DIR / f"motion_{ts}.jpg"
                            filepath.write_bytes(jpeg_bytes)
                            last_trigger = now

                            result = post_image(filepath)
                            if result is not None:
                                save_detection_result(filepath, result)
                            else:
                                with queue_lock:
                                    upload_queue.append(filepath)
                                try:
                                    send_wol(config["wol_mac"], config["wol_broadcast"])
                                except Exception:
                                    pass

                    prev_gray = curr_gray

                    if now - last_cleanup > 3600:
                        cleanup_old_captures()
                        last_cleanup = now

        except (urllib.error.URLError, OSError):
            time.sleep(5)
        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    main()
