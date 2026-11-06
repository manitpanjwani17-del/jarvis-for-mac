"""Jarvis's ears: the microphone, the wake word, and knowing when you've finished.

Audio stays in memory only. Nothing you say is saved to disk or sent anywhere.
"""
from __future__ import annotations

import queue
import re
import time
from typing import Callable, Iterable

import numpy as np
import sounddevice as sd

from .config import Config


class Microphone:
    """Streams 80 ms frames of 16 kHz mono audio into a queue."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.stream = None

    def _callback(self, indata, frames, time_info, status):
        try:
            self.frames.put_nowait(indata[:, 0].copy())
        except queue.Full:  # Jarvis fell behind; drop the oldest audio rather than lag
            try:
                self.frames.get_nowait()
                self.frames.put_nowait(indata[:, 0].copy())
            except queue.Empty:
                pass

    def start(self):
        self.stream = sd.InputStream(samplerate=self.cfg.sample_rate, channels=1, dtype="int16",
                                     blocksize=self.cfg.frame_samples, callback=self._callback)
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def flush(self):
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None


def level_of(frame: np.ndarray) -> float:
    """Loudness of a frame, 0..1, for the HUD."""
    if frame is None or len(frame) == 0:
        return 0.0
    rms = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768) ** 2)))
    return min(1.0, rms * 12)


class WakeWord:
    """Listens for "Hey Jarvis" using openWakeWord, entirely on this Mac."""

    def __init__(self, cfg: Config):
        from openwakeword.model import Model

        self.cfg = cfg.wake
        self.model = Model(wakeword_models=[str(self.cfg.model)], inference_framework="onnx",
                           melspec_model_path=str(self.cfg.melspec),
                           embedding_model_path=str(self.cfg.embedding))
        self.name = next(iter(self.model.models.keys()))
        self._above = 0
        self._quiet_frames = 0   # counted in audio frames, not clock time
        self._cooldown_frames = int(self.cfg.cooldown_s / 0.08)

    def reset(self):
        self.model.reset()
        self._above = 0

    def hear(self, frame: np.ndarray) -> float:
        """Feed one frame. Returns the score if the wake word was just heard, else 0."""
        score = float(self.model.predict(frame)[self.name])
        if self._quiet_frames > 0:
            self._quiet_frames -= 1
            return 0.0
        self._above = self._above + 1 if score >= self.cfg.threshold else 0
        if self._above >= self.cfg.patience:
            self._above = 0
            self._quiet_frames = self._cooldown_frames
            return score
        return 0.0


class SpeechDetector:
    """Silero VAD: tells speech from silence and background noise."""

    def __init__(self, cfg: Config):
        from openwakeword.vad import VAD

        self.vad = VAD(model_path=str(cfg.listen.vad_model))

    def reset(self):
        self.vad.reset_states()

    def prob(self, frame: np.ndarray) -> float:
        return float(self.vad.predict(frame, frame_size=640))


def record_command(frames: Iterable[np.ndarray] | Callable[[], np.ndarray | None], cfg: Config,
                   detector: SpeechDetector, on_level=lambda l: None,
                   still_talking: Callable[[np.ndarray], bool] | None = None) -> np.ndarray | None:
    """Collect what you say after waking Jarvis, stopping when you go quiet.

    Returns 16 kHz float32 audio, or None if you said nothing.
    """
    lc = cfg.listen
    frame_s = cfg.frame_samples / cfg.sample_rate
    detector.reset()
    pre_roll: list[np.ndarray] = []   # keeps the start of the first word
    captured: list[np.ndarray] = []
    started = False
    silence = 0.0
    waited = 0.0
    extended = 0
    get = frames if callable(frames) else iter(frames).__next__

    while True:
        try:
            frame = get()
        except StopIteration:
            break
        if frame is None:
            waited += 1.0
            if waited > lc.start_timeout_s:
                break
            continue
        on_level(level_of(frame))
        speech = detector.prob(frame) >= lc.speech_threshold
        if not started:
            pre_roll = (pre_roll + [frame])[-4:]
            if speech:
                started = True
                captured.extend(pre_roll)
            else:
                waited += frame_s
                if waited >= lc.start_timeout_s:
                    return None
            continue
        captured.append(frame)
        silence = 0.0 if speech else silence + frame_s
        if len(captured) * frame_s >= lc.max_command_s:
            break
        if silence >= lc.end_silence_s:
            # A pause. If you stopped mid-sentence ("tell Mum that…"), give you a little longer.
            if still_talking and extended < 2 and still_talking(np.concatenate(captured).astype(np.float32) / 32768):
                extended += 1
                silence = lc.end_silence_s - lc.extra_pause_s
                continue
            break

    if not started or not captured:
        return None
    audio = np.concatenate(captured).astype(np.float32) / 32768
    return audio


def make_transcriber(model_dir, compute_type="int8", cpu_threads=8):
    """A function (audio, prompt) -> text for a speech model folder: Apple MLX models (named mlx-…) run
    on the Mac's graphics chip; the others (faster-whisper) on the processor."""
    from pathlib import Path as _P
    model_dir = _P(model_dir)
    if model_dir.name.startswith("mlx-"):
        import mlx_whisper

        def run_mlx(audio, prompt=""):
            r = mlx_whisper.transcribe(np.asarray(audio, dtype=np.float32), path_or_hf_repo=str(model_dir), language="en",
                                       initial_prompt=prompt or None, condition_on_previous_text=False, temperature=0.0)
            return " ".join(s["text"].strip() for s in r.get("segments", []) if s.get("no_speech_prob", 0) < 0.7).strip()
        return run_mlx
    from faster_whisper import WhisperModel
    m = WhisperModel(str(model_dir), device="cpu", compute_type=compute_type, cpu_threads=cpu_threads)

    def run_fw(audio, prompt=""):
        segs, _ = m.transcribe(audio, language="en", beam_size=1, initial_prompt=prompt or None,
                               condition_on_previous_text=False, vad_filter=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.7).strip()
    return run_fw


def model_ready(d) -> bool:
    return (d / "model.bin").exists() or (d / "weights.safetensors").exists()


class Hearing:
    """Whisper speech-to-text, on this Mac. Large-v3 Turbo on the graphics chip by default (measured on 40
    real recorded sentences: 6.4% of words wrong in 0.55 s, against 9.3% in 0.85 s for small.en)."""

    def __init__(self, cfg: Config):
        from faster_whisper import WhisperModel
        from . import settings

        self.cfg = cfg.hearing
        folder = self.cfg.model_dir.parent
        chosen = settings.load().get("hearing_model")
        model_dir = next((d for d in (folder / chosen if chosen else None, self.cfg.preferred, self.cfg.model_dir)
                          if d is not None and model_ready(d)), self.cfg.model_dir)
        self.vocabulary = ""          # names Jarvis should expect (your people, groups); set by the assistant
        try:
            self._run = make_transcriber(model_dir, self.cfg.compute_type, self.cfg.cpu_threads)
            self.model_name = model_dir.name
        except Exception:
            self._run = make_transcriber(self.cfg.model_dir, self.cfg.compute_type, self.cfg.cpu_threads)
            self.model_name = self.cfg.model_dir.name
        self.checker = WhisperModel(str(self.cfg.wake_check_dir), device="cpu", compute_type=self.cfg.compute_type,
                                    cpu_threads=self.cfg.cpu_threads)
        # One silent pass each so the first real command isn't slow.
        self.transcribe(np.zeros(16000, dtype=np.float32))
        self.heard_name(np.zeros(16000, dtype=np.float32))

    def heard_name(self, audio: np.ndarray) -> bool:
        """Second check after the wake word fires: does the audio really say "Jarvis"?

        The wake-word model also reacts to sound-alikes such as "Hey Travis". Whisper,
        given no hint about what to expect, settles it.
        """
        segments, _ = self.checker.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                                            vad_filter=False, without_timestamps=True)
        text = " ".join(s.text for s in segments)
        self.last_check_word = closest_to_jarvis(text)
        return says_jarvis(text)

    DICTATION_PROMPT = "Hi, here's what I want to write, with proper punctuation."

    def transcribe(self, audio: np.ndarray, dictation: bool = False) -> str:
        """dictation: you are writing something (fn), not giving Jarvis a command."""
        base = self.DICTATION_PROMPT if dictation else self.cfg.prompt
        prompt = (base + " " + self.vocabulary).strip()[:600]
        text = self._run(audio, prompt)
        low = re.sub(r"\W+", " ", text.lower()).strip()
        if len(low) > 8 and low in re.sub(r"\W+", " ", prompt.lower()):
            return ""                 # Whisper repeating its list of expected words: nothing was said
        return _clean(text)


_NOT_JARVIS = {"travis", "harvest", "harvey", "jars", "service", "siri", "marvin", "there"}


def closest_to_jarvis(text: str) -> str:
    """The word in a transcript that sounds most like "Jarvis" (for the log when a wake-up is refused)."""
    import difflib
    import re as _re
    words = _re.findall(r"[a-z']+", text.lower())
    return max(words, key=lambda w: difflib.SequenceMatcher(None, w, "jarvis").ratio(), default="")


def says_jarvis(text: str) -> bool:
    """True if a transcript contains "Jarvis" or a usual mishearing of it.

    Accepted: Jarvis, Jervis, Javis, Jarvas, Charvis, Sharvis, Yarvis, Davis… (how speech recognition
    often writes "Jarvis" said with different accents). Refused: Travis, Harvest and other words that
    only share a few letters.
    """
    import difflib
    import re as _re

    for word in _re.findall(r"[a-z']+", text.lower()):
        if word in _NOT_JARVIS or len(word) < 4:
            continue
        ratio = difflib.SequenceMatcher(None, word, "jarvis").ratio()
        if word[0] in "jg" and ratio >= 0.6:
            return True
        if _re.match(r"^(ch|sh|y|d|z|dj)[aeiou]r?[vwb](i|e|a)s$", word):     # Charvis, Davis, Yarvis, Zarvis
            return True
    return False


def use_model(hearing: "Hearing", model_dir) -> None:
    """Switch the speech model (after voice training picks the one that hears you best)."""
    hearing._run = make_transcriber(model_dir, hearing.cfg.compute_type, hearing.cfg.cpu_threads)
    hearing.model_name = model_dir.name


# Whisper sometimes "hears" these in noise or silence.
_PHANTOMS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", ".", "bye.",
             "jarvis, sir, whatsapp, imessage, spotify, safari."}


def _clean(text: str) -> str:
    if text.lower().strip() in _PHANTOMS:
        return ""
    return text
