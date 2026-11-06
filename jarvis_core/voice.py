"""Jarvis's voice.

Kokoro (an open, Apache-licensed neural voice model) speaks with a blend of its
British male voices. A light finishing chain gives it a crisp, composed, slightly
synthetic quality in the style of a film AI butler. It is an original voice: it is
not a copy of any actor's voice.

Common phrases ("Yes, Sir?") are made once and kept in cache/voice, so Jarvis
answers instantly. Anything new is made on the spot in under a second.
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy import signal
from scipy.io import wavfile

from .config import VoiceConfig

SR = 24000  # Kokoro's output rate


# ---------- finishing chain (RBJ biquads) ----------

def _peaking(f0, gain_db, q, sr=SR):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    alpha = np.sin(w) / (2 * q)
    b = [1 + alpha * a, -2 * np.cos(w), 1 - alpha * a]
    den = [1 + alpha / a, -2 * np.cos(w), 1 - alpha / a]
    return np.array(b) / den[0], np.array(den) / den[0]


def _shelf(f0, gain_db, high=True, sr=SR, s=0.8):
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * f0 / sr
    cw, sw = np.cos(w), np.sin(w)
    alpha = sw / 2 * np.sqrt((a + 1 / a) * (1 / s - 1) + 2)
    sq = 2 * np.sqrt(a) * alpha
    if high:
        b = [a * ((a + 1) + (a - 1) * cw + sq), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - sq)]
        d = [(a + 1) - (a - 1) * cw + sq, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - sq]
    else:
        b = [a * ((a + 1) - (a - 1) * cw + sq), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - sq)]
        d = [(a + 1) + (a - 1) * cw + sq, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - sq]
    return np.array(b) / d[0], np.array(d) / d[0]


def _room(sr=SR, seed=7):
    """A very small, bright room: a few early reflections and a short tail."""
    rng = np.random.default_rng(seed)
    n = int(0.22 * sr)
    ir = np.zeros(n, dtype=np.float32)
    for ms, g in [(7, 0.35), (11, 0.27), (17, 0.2), (23, 0.16), (31, 0.12), (43, 0.08)]:
        ir[int(ms * sr / 1000)] += g
    t = np.arange(n) / sr
    ir += (rng.standard_normal(n) * np.exp(-t / 0.05) * 0.06).astype(np.float32)
    ir[: int(0.005 * sr)] = 0
    return ir


_ROOM = _room()


def polish(x: np.ndarray, amount: float = 1.0) -> np.ndarray:
    """The JARVIS-style finish. amount 0 returns the voice untouched."""
    if amount <= 0 or len(x) == 0:
        return x
    y = x.astype(np.float64)
    b, a = signal.butter(2, 85, "highpass", fs=SR)                 # remove rumble
    y = signal.lfilter(b, a, y)
    for b, a in (_peaking(280, -2.0 * amount, 1.0),                # less boxiness
                 _peaking(3300, 2.5 * amount, 0.9),                # presence: crisp diction
                 _shelf(9000, 2.5 * amount, high=True),            # air: polished, synthetic sheen
                 _shelf(140, 1.5 * amount, high=False)):           # a little chest warmth
        y = signal.lfilter(b, a, y)
    # A faint doubled copy 9 ms later: the subtle "electronic" edge.
    d = int(0.009 * SR)
    doubled = np.concatenate([np.zeros(d), y[:-d]]) if len(y) > d else y
    y = y + 0.10 * amount * doubled
    # The small room.
    wet = signal.fftconvolve(y, _ROOM)[: len(y)]
    y = y + 0.16 * amount * wet
    # Gentle compression so quiet words stay clear.
    env = np.abs(signal.hilbert(y)) + 1e-6
    env = signal.filtfilt(*signal.butter(1, 12, fs=SR), env)
    thresh = 0.25
    gain = np.where(env > thresh, (thresh + (env - thresh) / 2.5) / env, 1.0)
    y = y * gain
    # Level: about -18 dBFS RMS, peaks kept below -1 dBFS.
    rms = np.sqrt(np.mean(y ** 2)) + 1e-9
    y = y * (0.125 / rms)
    peak = np.max(np.abs(y))
    if peak > 0.89:
        y = y * (0.89 / peak)
    return y.astype(np.float32)


# ---------- the voice ----------

class Voice:
    def __init__(self, cfg: VoiceConfig, on_level=None):
        from kokoro_onnx import Kokoro

        self.cfg = cfg
        self.on_level = on_level or (lambda level: None)
        self.kokoro = Kokoro(str(cfg.model), str(cfg.voices))
        voices = np.load(cfg.voices)  # numeric arrays only; no code can be loaded
        total = sum(cfg.blend.values())
        self.style = sum(voices[name] * (w / total) for name, w in cfg.blend.items())
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stop = threading.Event()
        self.speaking = False
        self.output = None      # the echo-cancelling helper, when running (audio_io.DuplexAudio)
        self._lock = threading.Lock()

    # -- making audio --

    def _key(self, text: str) -> str:
        sig = f"{text}|{sorted(self.cfg.blend.items())}|{self.cfg.speed}|{self.cfg.polish}|v2"
        return hashlib.sha1(sig.encode()).hexdigest()[:16]

    def render(self, text: str, use_cache: bool = True) -> np.ndarray:
        path = self.cfg.cache_dir / f"{self._key(text)}.wav"
        if use_cache and path.exists():
            _, data = wavfile.read(path)
            return data.astype(np.float32) / 32767
        with self._lock:  # Kokoro is not built for two callers at once
            audio, sr = self.kokoro.create(text, voice=self.style, speed=self.cfg.speed, lang="en-gb")
        audio = polish(np.asarray(audio, dtype=np.float32), self.cfg.polish)
        # A short fade at each end so playback never clicks.
        f = min(len(audio) // 4, int(0.006 * SR))
        if f > 0:
            ramp = np.linspace(0, 1, f, dtype=np.float32)
            audio[:f] *= ramp
            audio[-f:] *= ramp[::-1]
        if use_cache:
            wavfile.write(path, SR, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
        return audio

    def warm(self, phrases) -> None:
        """Make the stock phrases ahead of time so Jarvis answers instantly."""
        for p in phrases:
            self.render(p)

    # -- playing audio --

    def play(self, audio: np.ndarray) -> bool:
        """Play audio, reporting its loudness for the HUD. Returns False if stopped early."""
        self._stop.clear()
        self.speaking = True
        audio = (audio * self.cfg.volume).astype(np.float32)
        if self.output is not None and self.output.alive:
            try:
                return self.output.play(audio, self.on_level, self._stop)
            finally:
                self.speaking = False
        pos = 0
        block = int(0.03 * SR)
        try:
            with sd.OutputStream(samplerate=SR, channels=1, dtype="float32", blocksize=block) as out:
                while pos < len(audio):
                    if self._stop.is_set():
                        return False
                    chunk = audio[pos: pos + block]
                    out.write(chunk.reshape(-1, 1))
                    rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
                    self.on_level(min(1.0, rms * 6))
                    pos += block
                time.sleep(0.05)
            return True
        finally:
            self.speaking = False
            self.on_level(0.0)

    def say(self, text: str, use_cache: bool = True) -> bool:
        return self.play(self.render(text, use_cache))

    def stop(self) -> None:
        self._stop.set()
        if self.output is not None and self.output.alive:
            self.output.stop_speaking()


def chime(kind: str = "wake") -> np.ndarray:
    """Short original tones: a rising two-note chime on waking, falling when done."""
    notes = {"wake": [(880, 0.07), (1320, 0.11)], "done": [(1175, 0.07), (784, 0.10)],
             "error": [(330, 0.09), (262, 0.14)]}[kind]
    out = []
    for f, d in notes:
        t = np.arange(int(d * SR)) / SR
        env = np.minimum(1, t / 0.006) * np.exp(-t / (d * 0.55))
        tone = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(2 * np.pi * 2 * f * t)
        out.append(tone * env)
    return (np.concatenate(out) * 0.18).astype(np.float32)
