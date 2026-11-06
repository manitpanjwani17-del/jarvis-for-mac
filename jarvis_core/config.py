"""Every setting Jarvis uses, in one place.

Change values here, not in the code that uses them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .brain import BrainConfig

from .paths import CACHE, LOGS, MODELS, OVERLAY


@dataclass
class WakeConfig:
    model: Path = MODELS / "wakeword" / "hey_jarvis_v0.1.onnx"
    melspec: Path = MODELS / "wakeword" / "melspectrogram.onnx"
    embedding: Path = MODELS / "wakeword" / "embedding_model.onnx"
    # 0..1. Higher means fewer false wake-ups but you may need to say it more clearly.
    threshold: float = 0.5
    # Frames (80 ms each) the score must stay above the threshold. Guards against
    # a single noisy frame waking Jarvis.
    patience: int = 2
    # When the wake-word model is at least this sure, trust it even if the second check mishears the name
    # (echo cancellation can make "Jarvis" come out as "Harris" or "Yaris"). A clearly different name
    # ("Travis") is still refused.
    trust_score: float = 0.9
    # Ignore further wake words for this long after one is heard.
    cooldown_s: float = 1.5


@dataclass
class ListenConfig:
    vad_model: Path = MODELS / "wakeword" / "silero_vad.onnx"
    # Silero speech probability above which a frame counts as speech.
    speech_threshold: float = 0.5
    # How long to wait for you to start talking after "Yes, Sir?".
    start_timeout_s: float = 6.0
    # This much silence after you stop talking ends the command…
    end_silence_s: float = 0.8
    # …unless you seem to have stopped mid-sentence ("…and"), when he waits this much longer.
    smart_end: bool = True
    extra_pause_s: float = 1.0
    # Talking over Jarvis interrupts him (needs the echo-cancelling helper).
    barge_in: bool = True
    barge_threshold: float = 0.6       # how sure the speech detector must be that it's a voice
    barge_frames: int = 4              # …for this many 80 ms frames in a row (about a third of a second)
    barge_check_s: float = 0.5         # then he lowers his voice and listens this long to decide
    # A command can never be longer than this.
    max_command_s: float = 15.0


@dataclass
class HearingConfig:
    # Measured on 40 real recorded sentences on this Mac: Whisper Large-v3 Turbo on the graphics chip (Apple MLX)
    # gets 6.4% of words wrong in 0.55 s; small.en on the processor 9.3% in 0.85 s. Turbo is used when present;
    # small.en is the fallback. Voice training re-checks both on your own voice.
    preferred: Path = MODELS / "hearing" / "mlx-whisper-large-v3-turbo"
    model_dir: Path = MODELS / "hearing" / "faster-whisper-small.en"
    # A smaller, faster model for the split-second "did they really say Jarvis?" check.
    wake_check_dir: Path = MODELS / "hearing" / "faster-whisper-base.en"
    cpu_threads: int = 8
    # Voice training keeps the most accurate installed model that takes at most this long per sentence.
    max_seconds: float = 1.6
    compute_type: str = "int8"
    beam_size: int = 1
    # Words Whisper should expect, so it spells them right.
    prompt: str = "Jarvis, Sir, WhatsApp, iMessage, Spotify, Safari."


@dataclass
class VoiceConfig:
    model: Path = MODELS / "voice" / "kokoro-v1.0.onnx"
    voices: Path = MODELS / "voice" / "voices-v1.0.bin"
    # A blend of Kokoro's British male voices. Weights must add up to 1.
    blend: dict = field(default_factory=lambda: {"bm_george": 0.7, "bm_lewis": 0.3})
    speed: float = 1.04
    # The JARVIS-style finish: presence, air and a faint room. 0 turns it off.
    polish: float = 1.0
    volume: float = 0.9
    cache_dir: Path = CACHE / "voice"


@dataclass
class SafetyConfig:
    # Network guard: only this Mac, plus ordinary web connections for lookups you ask for
    # (allow_web). Everything else is refused.
    offline: bool = True
    allow_web: bool = True
    # Spoken answers that count as "yes" to a confirmation. Anything else is "no".
    yes_words: tuple = ("yes", "yeah", "yep", "yup", "sure", "do it", "send it", "go ahead",
                        "confirm", "confirmed", "affirmative", "please do", "correct", "ok", "okay")
    no_words: tuple = ("no", "nope", "don't", "do not", "cancel", "stop", "wait", "never mind",
                       "nevermind", "not", "hold on", "abort")
    confirm_timeout_s: float = 8.0
    log_file: Path = LOGS / "actions.log"
    # Whether to write what you said into the log. Off keeps your words out of it.
    log_commands: bool = True
    lock_file: Path = LOGS / "jarvis.pid"


@dataclass
class MessagingConfig:
    default_app: str = "whatsapp"      # "whatsapp" or "imessage"
    country_code: str = ""             # added to numbers saved without one; set at setup ("my country code is 44")
    # The action log records who you messaged, never what the message said.
    log_text: bool = False
    max_edits: int = 3                 # "change it to…" at most this many times per message
    per_minute: int = 3
    per_hour: int = 20
    check_every_s: float = 4.0         # how often to look for new messages, when reading them aloud is on


@dataclass
class HUDConfig:
    enabled: bool = True
    binary: Path = OVERLAY / "JarvisOverlay"
    source: Path = OVERLAY / "JarvisOverlay.swift"


@dataclass
class Config:
    name_for_user: str = "Sir"
    sample_rate: int = 16000
    frame_samples: int = 1280  # 80 ms at 16 kHz, the size the wake-word model wants
    wake: WakeConfig = field(default_factory=WakeConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    hearing: HearingConfig = field(default_factory=HearingConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    hud: HUDConfig = field(default_factory=HUDConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    # After greeting you and after every reply, he listens this long for more, without "Hey Jarvis"…
    follow_up_s: float = 4.0
    # …at most this many times in a row.
    brain_follow_ups: int = 6
