"""Microphone and speaker through one echo-cancelling helper (helpers/JarvisAudio).

With this, Jarvis hears you while he is talking: macOS's voice processing removes his own voice
from what the microphone picks up, so you can interrupt him. If the helper can't start, Jarvis
falls back to the plain microphone and speaker (and can then only be interrupted by "Hey Jarvis").

Audio only passes through; nothing is recorded or saved.
"""
from __future__ import annotations

import json
import queue
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from .paths import HELPERS
HELPER = HELPERS / "JarvisAudio"
VOICE_RATE = 24000


class DuplexAudio:
    """Looks like the Microphone (read / flush / start / stop) and also plays Jarvis's voice."""

    def __init__(self, frame_samples: int = 1280):
        self.frame_bytes = frame_samples * 2
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.proc: subprocess.Popen | None = None
        self.aec = False
        self._ready = threading.Event()
        self._played = 0
        self._sent = 0
        self._stopped_at = -1
        self._write = threading.Lock()
        self.failed = ""
        self.events: list[dict] = []
        self.setup = ""

    # ---------- life ----------

    def start(self, timeout: float = 8.0) -> bool:
        if not HELPER.exists():
            self.failed = "helper missing"
            return False
        self.proc = subprocess.Popen([str(HELPER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read_events, daemon=True).start()
        threading.Thread(target=self._read_mic, daemon=True).start()
        if not self._ready.wait(timeout) or self.proc.poll() is not None:
            self.failed = self.failed or "didn't start"
            self.stop()
            return False
        return True

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def _read_events(self):
        for line in self.proc.stderr:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ready"):
                self.aec = bool(e.get("aec"))
                self.setup = e.get("setup", "")
                self._ready.set()
            elif "played" in e:
                self._played = max(self._played, int(e["played"]))
            elif e.get("stopped"):
                self._stopped_at = self._sent
            elif e.get("error") or e.get("warning"):
                self.failed = (e.get("error") or e.get("warning")) + (f" ({e['detail']})" if e.get("detail") else "")
                self.events.append(e)

    def _read_mic(self):
        buf = b""
        stream = self.proc.stdout
        while True:
            chunk = stream.read(self.frame_bytes - len(buf))
            if not chunk:
                break
            buf += chunk
            if len(buf) < self.frame_bytes:
                continue
            frame = np.frombuffer(buf, dtype=np.int16).copy()
            buf = b""
            try:
                self.frames.put_nowait(frame)
            except queue.Full:            # fell behind: drop the oldest rather than lag
                try:
                    self.frames.get_nowait()
                    self.frames.put_nowait(frame)
                except queue.Empty:
                    pass

    # ---------- microphone side (same as Microphone) ----------

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self):
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    # ---------- speaker side ----------

    def _send(self, kind: bytes, payload: bytes = b""):
        with self._write:
            self.proc.stdin.write(kind + struct.pack("<I", len(payload)) + payload)
            self.proc.stdin.flush()

    def set_volume(self, v: float):
        if self.alive:
            self._send(b"V", struct.pack("<f", float(max(0.0, min(1.0, v)))))

    def stop_speaking(self):
        if self.alive:
            self._send(b"S")

    def play(self, audio: np.ndarray, on_level=lambda l: None, stop: threading.Event | None = None) -> bool:
        """Play and wait until it has been heard. Returns False if stopped early."""
        if not self.alive or len(audio) == 0:
            return False
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        self.set_volume(1.0)
        self._sent += 1
        mine = self._sent
        self._send(b"P", audio.tobytes())
        block = int(0.03 * VOICE_RATE)
        levels = [float(np.sqrt(np.mean(audio[i:i + block] ** 2))) for i in range(0, len(audio), block)]
        start = time.monotonic()
        limit = len(audio) / VOICE_RATE + 3.0
        try:
            while self._played < mine:
                if stop is not None and stop.is_set():
                    self.stop_speaking()
                    return False
                if self._stopped_at >= mine:
                    return False
                elapsed = time.monotonic() - start
                if elapsed > limit:
                    break
                i = int(elapsed / 0.03)
                on_level(min(1.0, levels[i] * 6) if i < len(levels) else 0.0)
                time.sleep(0.03)
            return True
        finally:
            on_level(0.0)
