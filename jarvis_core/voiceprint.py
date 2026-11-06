"""Recognising your voice, so Jarvis listens to you and not to whoever else is talking.

You train it once by saying a few sentences ("Hey Jarvis, learn my voice"). Each sentence becomes a
voiceprint: 192 numbers describing how your voice sounds, not what you said. Their average is kept in
data/voiceprint.npz. The recordings are thrown away straight away; no audio is ever stored.

Afterwards, whenever Jarvis hears "Hey Jarvis", a command, or someone talking over him, he makes a
voiceprint of that moment and compares it with yours. Too different, and he ignores it.

Model: NVIDIA TitaNet-Large, run by sherpa-onnx on this Mac (about 65 ms per check).
Chosen by test on 40 real speakers (LibriSpeech dev-clean), against three other models. With a
reference average voice subtracted first (models/speaker/titanet_reference_mean.npy, numbers only):
  phrases of 3 s or more: you accepted 100%, strangers rejected 100% (cut-off 0.40)
  phrases of about 1 s:   you accepted 98%,  strangers rejected 100% (cut-off 0.30)
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from .paths import ASSETS, DATA, MODELS
MODEL = MODELS / "speaker" / "nemo_en_titanet_large.onnx"
REFERENCE = ASSETS / "titanet_reference_mean.npy"   # made by this project from LibriSpeech; numbers only
FILE = DATA / "voiceprint.npz"

LONG_S, SHORT_S = 2.5, 1.0          # speech length where the strict / lenient cut-off applies
CUT_LONG, CUT_SHORT = 0.40, 0.30


class VoicePrint:
    def __init__(self, model: Path = MODEL, file: Path | None = None, reference: Path = REFERENCE):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2, debug=False, provider="cpu")
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.file = file or FILE          # looked up when made, so tests can point it elsewhere
        try:
            self.mean = np.load(reference).astype(np.float32)
        except OSError:
            self.mean = None
        self.me: np.ndarray | None = None
        self.adjust = 0.0             # made more lenient at training if your own sentences vary a lot
        self.load()

    # ---------- voiceprints ----------

    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        """16 kHz float audio -> a normalised voiceprint (None if there's too little to judge)."""
        audio = trim(np.asarray(audio, dtype=np.float32))
        if len(audio) < 16000 * 0.45:
            return None
        s = self.extractor.create_stream()
        s.accept_waveform(sample_rate=16000, waveform=audio)
        s.input_finished()
        if not self.extractor.is_ready(s):
            return None
        e = np.array(self.extractor.compute(s), dtype=np.float32)
        n = np.linalg.norm(e)
        if n == 0:
            return None
        e = e / n                                  # scale to length 1 first (the reference was made this way),
        if self.mean is not None and self.mean.shape == e.shape:
            e = e - self.mean                      # then take away what every voice has in common,
        n = np.linalg.norm(e)
        return e / n if n > 0 else None            # and scale again

    @staticmethod
    def cut_off(seconds: float) -> float:
        if seconds >= LONG_S:
            return CUT_LONG
        if seconds <= SHORT_S:
            return CUT_SHORT
        return CUT_SHORT + (CUT_LONG - CUT_SHORT) * (seconds - SHORT_S) / (LONG_S - SHORT_S)

    # ---------- your voice ----------

    @property
    def trained(self) -> bool:
        return self.me is not None

    def enroll(self, clips: list[np.ndarray]) -> dict:
        embs = [e for e in (self.embed(c) for c in clips) if e is not None]
        if len(embs) < 4:
            raise ValueError("not enough clear speech")
        me = np.mean(embs, axis=0)
        me /= np.linalg.norm(me)
        sims = [float(e @ me) for e in embs]
        # If even your own training sentences differ a lot, be a little more lenient (never below 0.22).
        self.adjust = float(np.clip(min(sims) - 0.50, -0.08, 0.0))
        self.me = me
        self.save(len(embs))
        return {"sentences": len(embs), "consistency": round(float(np.mean(sims)), 2), "lenience": round(-self.adjust, 2)}

    def check(self, audio: np.ndarray) -> tuple[bool | None, float | None]:
        """(is it you?, similarity). None = not trained, or too little speech to tell."""
        if self.me is None:
            return None, None
        e = self.embed(audio)
        if e is None:
            return None, None
        s = float(e @ self.me)
        secs = len(trim(np.asarray(audio, dtype=np.float32))) / 16000
        return s >= max(0.22, self.cut_off(secs) + self.adjust), s

    # ---------- keeping it ----------

    def save(self, sentences: int):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        np.savez(self.file, me=self.me, adjust=self.adjust, sentences=sentences, when=time.time(), model="titanet_large")

    def load(self):
        try:
            d = np.load(self.file)            # numbers only; nothing that can run
            if str(d.get("model", "")) != "titanet_large":
                self.me = None                 # trained with an older model: needs training again
                return
            self.me, self.adjust = d["me"].astype(np.float32), float(d["adjust"])
        except (OSError, KeyError, ValueError):
            self.me = None

    def forget(self):
        self.me = None
        self.file.unlink(missing_ok=True)


def trim(audio: np.ndarray, frame: int = 320) -> np.ndarray:
    """Drop quiet stretches at the start and end, so silence doesn't dilute the voiceprint."""
    if len(audio) < frame * 4:
        return audio
    n = len(audio) // frame
    e = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    loud = np.where(e > max(0.004, np.percentile(e, 90) * 0.12))[0]
    if len(loud) == 0:
        return audio[:0]
    return audio[max(0, loud[0] - 2) * frame: min(n, loud[-1] + 3) * frame]
