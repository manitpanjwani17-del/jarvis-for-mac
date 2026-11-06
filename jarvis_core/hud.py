"""Talks to the on-screen HUD helper (overlay/JarvisOverlay).

If the helper is missing or crashes, Jarvis carries on without it: the HUD is
for show, never something Jarvis depends on.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time

from .config import HUDConfig


class HUD:
    def __init__(self, cfg: HUDConfig):
        self.cfg = cfg
        self.proc = None
        self._lock = threading.Lock()
        self._last_level_sent = 0.0
        if cfg.enabled:
            self._start()

    def _start(self):
        if not self.cfg.binary.exists() or (
                self.cfg.source.exists() and self.cfg.source.stat().st_mtime > self.cfg.binary.stat().st_mtime):
            try:
                subprocess.run(["swiftc", "-O", "-o", str(self.cfg.binary), str(self.cfg.source)],
                               check=True, capture_output=True, timeout=300)
            except Exception:
                return
        try:
            self.proc = subprocess.Popen([str(self.cfg.binary)], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            self.proc = None

    def send(self, **msg):
        if not self.proc or self.proc.poll() is not None:
            return
        with self._lock:
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.proc = None

    def show(self, state: str, text: str | None = None, subtext: str | None = None):
        from .persona import addressed
        msg = {"state": state}
        if text is not None:
            msg["text"] = addressed(text)
        if subtext is not None:
            msg["subtext"] = addressed(subtext)
        self.send(**msg)

    def level(self, value: float):
        now = time.monotonic()
        if now - self._last_level_sent >= 0.03:   # at most ~30 updates a second
            self._last_level_sent = now
            self.send(level=round(float(value), 3))

    def hide(self):
        self.send(state="hidden", text="", subtext="")

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None
