#!/usr/bin/env python3
"""MJPEG over HTTP from rpicam-vid.  Multi-client: one capture process, many viewers."""

import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CMD = [
    "rpicam-vid",
    "--codec", "mjpeg",
    "--nopreview",
    "--rotation", "180",
    "--width", "1280",
    "--height", "720",
    "--framerate", "10",
    "--quality", "75",
    "-t", "0",
    "-o", "-",
]

class FrameBroadcaster:
    """Runs one rpicam-vid process and fans frames out to all subscribers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None
        self._event = threading.Event()
        self._proc = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._capture, daemon=True)
        self._thread.start()

    def _capture(self):
        while True:
            self._proc = subprocess.Popen(
                CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0
            )
            try:
                for jpg in self._extract_jpegs(self._proc.stdout):
                    with self._lock:
                        self._frame = jpg
                    self._event.set()
                    self._event.clear()
            except Exception:
                pass
            finally:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            time.sleep(1)

    @staticmethod
    def _extract_jpegs(stream):
        buf = b""
        while True:
            chunk = stream.read(8192)
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

    def subscribe(self):
        """Yield JPEG frames as they arrive. Blocks between frames."""
        seq = None
        while True:
            self._event.wait(timeout=2)
            with self._lock:
                frame = self._frame
            if frame is not None and frame is not seq:
                seq = frame
                yield frame


broadcaster = FrameBroadcaster()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path not in ("/", "/cam.mjpg"):
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

        part = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        try:
            for jpg in broadcaster.subscribe():
                self.wfile.write(part)
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


if __name__ == "__main__":
    broadcaster.start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
