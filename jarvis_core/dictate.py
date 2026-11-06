"""fn dictation: hold fn, speak, let go, and the cleaned-up text is typed where you are typing.

    hold fn            push-to-talk: recording while fn is held, typed when you let go
    tap fn twice       hands-free: recording until you tap fn once more (Esc cancels)
    fn + another key   not dictation (fn+F5, fn+arrows…): forgotten
    no text box        the text is copied, and shown on a card with a Copy button

Recording starts the moment fn goes down, so the first word is never clipped. Long dictation is
transcribed piece by piece at your pauses while you are still talking, so only the last bit is
left when you let go. Then polish.py cleans it up, and helpers/JarvisDictate types it.

Privacy: everything stays on this Mac. Jarvis never reads what is in the text box, only what kind
of box it is; never presses Return or sends anything; never types into a password box; puts your
clipboard back after pasting; and logs only the app, the number of words and how it was delivered,
never the words. The last text is kept in memory for five minutes ("paste that again"), never on disk.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import safety, settings
from .ears import level_of
from .polish import Polisher

from .paths import CODE, HELPERS, PACKAGED
HELPER: Path | None = HELPERS / "JarvisDictate"
SOURCE = CODE / "helpers" / "JarvisDictate.swift"

PAUSE_S = 0.7            # a pause this long lets Jarvis start writing what came before it
MIN_PIECE_S = 3.0        # …if at least this much has been said since the last piece
MAX_HOLD_S = 300         # one dictation can't run longer than this
MAX_HANDSFREE_S = 360
REPASTE_S = 300          # "paste that again" works for this long


class DictationRequested(BaseException):
    """fn was pressed while Jarvis was talking or listening: stop that, and dictate.

    A BaseException, so no `except Exception` on the way swallows it; Assistant.run catches it."""


class Helper:
    """The JarvisDictate process: fn events in, commands out."""

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else HELPER
        self.proc: subprocess.Popen | None = None
        self.events: queue.Queue = queue.Queue()
        self.replies: queue.Queue = queue.Queue()
        self.ready = False
        self.error = ""
        self._lock = threading.Lock()
        self._down = threading.Event()        # an fn press is waiting to be acted on
        self.started_at = 0.0

    def start(self) -> bool:
        if self.path is None:
            return False
        try:
            if not self.path.exists() or (SOURCE.exists() and SOURCE.stat().st_mtime > self.path.stat().st_mtime):
                subprocess.run(["swiftc", "-O", "-swift-version", "5", "-o", str(self.path), str(SOURCE)],
                               check=True, capture_output=True, timeout=300)
            self.proc = subprocess.Popen([str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception as e:
            safety.log_error("starting fn dictation", e)
            self.proc = None
            return False
        self.started_at = time.monotonic()
        threading.Thread(target=self._read, daemon=True).start()
        return True

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _read(self):
        proc = self.proc
        for line in proc.stdout:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if "fn" in obj:
                if obj["fn"] in ("down", "handsfree"):
                    self._down.set()
                self.events.put(obj["fn"])
            elif "ready" in obj:
                self.ready, self.error = True, ""
            elif "error" in obj:
                self.error = obj["error"]
            else:
                self.replies.put(obj)

    def send(self, **msg):
        if not self.alive:
            return
        with self._lock:
            try:
                self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def request(self, key: str, timeout: float = 1.5, **msg) -> dict | None:
        while not self.replies.empty():
            self.replies.get_nowait()
        self.send(**msg)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                r = self.replies.get(timeout=max(0.01, end - time.monotonic()))
            except queue.Empty:
                break
            if key in r:
                return r[key]
        return None

    def requested(self) -> bool:
        return self._down.is_set()

    def next_event(self, timeout: float = 0.0) -> str | None:
        try:
            e = self.events.get(timeout=timeout) if timeout else self.events.get_nowait()
        except queue.Empty:
            return None
        if self.events.empty():
            self._down.clear()
        return e

    def pill(self, state: str, text: str = ""):
        self.send(cmd="pill", state=state, text=text)

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except Exception:
                self.proc.kill()
            self.proc = None


class Dictation:
    def __init__(self, assistant, helper: Helper | None = None, polisher: Polisher | None = None):
        self.a = assistant
        self.helper = helper or Helper()
        self.polisher = polisher or Polisher(url=assistant.cfg.brain.url)
        self.last_text = ""
        self.last_at = 0.0
        self._restarted = 0.0
        self.results: list[dict] = []          # for tests: how each dictation was delivered (no text)

    def start(self) -> bool:
        ok = self.helper.start()
        if ok:
            self.polisher.warm()
        return ok

    def check(self):
        """Restart the helper if it died (at most every 10 s)."""
        if self.helper.path is None or self.helper.alive or time.monotonic() - self._restarted < 10:
            return
        self._restarted = time.monotonic()
        safety.log("dictation_restart")
        self.helper.start()

    def requested(self) -> bool:
        return self.helper.requested()

    def status(self) -> dict:
        s = self.helper.request("status", cmd="status") or {}
        return {"running": self.helper.alive, "fn_key": bool(s.get("tap")), "accessibility": bool(s.get("accessibility")),
                "globe_do_nothing": s.get("globe") == 0, "error": self.helper.error}

    # ---------- one dictation ----------

    def take(self, recent: list[np.ndarray] | None = None):
        """Called from Jarvis's loop: act on whatever fn did."""
        while True:
            e = self.helper.next_event()
            if e is None:
                return
            if e in ("down", "handsfree"):
                if not settings.load().get("dictation", True):
                    continue
                self.run("handsfree" if e == "handsfree" else "hold", recent or [])
                recent = []

    def run(self, mode: str, recent: list[np.ndarray]):
        a, h = self.a, self.helper
        cfg = a.cfg
        frame_s = cfg.frame_samples / cfg.sample_rate
        try:
            a.voice.stop()
        except Exception:
            pass
        if hasattr(a.mic, "set_volume"):
            a.mic.set_volume(1.0)
        a.hud.hide()
        h.pill("handsfree" if mode == "handsfree" else "listening")
        if time.monotonic() - self.last_at > 1500:
            self.polisher.warm()                         # the editing model may have been unloaded
        frames: list[np.ndarray] = list(recent[-4:])     # about 0.3 s from just before fn went down
        a.detector.reset()
        pieces: list = []
        pool = ThreadPoolExecutor(max_workers=1)
        piece_start, quiet, spoke_since_cut, any_speech = 0, 0, 0, 0
        started = time.monotonic()
        outcome = None
        try:
            while outcome is None:
                f = a.mic.read(timeout=0.1)
                if f is not None:
                    frames.append(f)
                    h.send(level=round(level_of(f), 3))
                    speech = a.detector.prob(f) >= cfg.listen.speech_threshold
                    if speech:
                        quiet, spoke_since_cut, any_speech = 0, spoke_since_cut + 1, any_speech + 1
                    else:
                        quiet += 1
                    # A pause after a good stretch of speech: start writing that stretch now.
                    if (quiet * frame_s >= PAUSE_S and spoke_since_cut
                            and (len(frames) - piece_start) * frame_s >= MIN_PIECE_S):
                        cut = len(frames) - quiet + 3
                        pieces.append(pool.submit(self._hear, frames[piece_start:cut]))
                        piece_start, spoke_since_cut = cut, 0
                elif getattr(a.mic, "finished", False):
                    outcome = "finish"
                limit = MAX_HANDSFREE_S if mode == "handsfree" else MAX_HOLD_S
                if time.monotonic() - started > limit:
                    outcome = "finish"
                e = h.next_event()
                if e in ("up", "stop"):
                    outcome = "finish"
                elif e in ("tap", "cancel"):
                    outcome = e
                elif e == "handsfree":
                    mode = "handsfree"
                    h.pill("handsfree")
            if outcome != "finish":
                h.pill("hidden")
                safety.log("dictation", outcome="cancelled" if outcome == "cancel" else "tap")
                return
            h.pill("working")
            where = h.request("target", cmd="target") or {}
            tail = frames[piece_start:]
            if spoke_since_cut or not pieces:
                pieces.append(pool.submit(self._hear, tail))
            raw = " ".join(t for t in (p.result() for p in pieces) if t).strip()
        finally:
            pool.shutdown(wait=False)
            a.mic.flush() if hasattr(a.mic, "flush") else None
            a.wake.reset()
        if not raw or not any_speech:
            h.pill("nothing", "Didn't catch that")
            safety.log("dictation", outcome="nothing heard")
            return
        level = settings.load().get("dictation_polish", "full")
        text = self.polisher.polish(raw, where.get("app", ""), level)
        if not text:
            h.pill("nothing", "Didn't catch that")
            return
        self.deliver(text, first_look=where)
        safety.log("dictation", app=where.get("app", ""), words=len(text.split()), layer=self.polisher.last.get("layer"), why=self.polisher.last.get("why", ""),
                   to=self.results[-1]["to"], box=where.get("why", ""), seconds=round(len(frames) * frame_s, 1),
                   after_release_ms=round((time.monotonic() - started - len(frames) * frame_s) * 1000))

    def _hear(self, frames: list[np.ndarray]) -> str:
        if not frames:
            return ""
        audio = np.concatenate(frames).astype(np.float32) / 32768
        if len(audio) < 16000 * 0.3:
            return ""
        return self.a.hearing.transcribe(audio, dictation=True)

    def deliver(self, text: str, first_look: dict | None = None):
        """Type it where you are typing; if there is no text box, copy it and show it on a card."""
        h = self.helper
        where = h.request("target", cmd="target") or first_look or {}   # where the cursor is now
        self.last_text, self.last_at = text, time.monotonic()
        if where.get("secure"):
            h.request("card", cmd="card", text=text)
            h.pill("error", "Not typing into a password box")
            self.results.append({"to": "card (password box)", "app": where.get("app", "")})
        elif where.get("editable"):
            h.request("pasted", cmd="paste", text=text)
            h.pill("done", "Typed")
            self.results.append({"to": "typed", "app": where.get("app", "")})
        else:
            h.request("card", cmd="card", text=text)
            h.pill("hidden")
            self.results.append({"to": "card", "app": where.get("app", "")})

    def paste_again(self) -> bool:
        if not self.last_text or time.monotonic() - self.last_at > REPASTE_S:
            self.last_text = ""
            return False
        self.deliver(self.last_text)
        return True

    def close(self):
        self.helper.close()
