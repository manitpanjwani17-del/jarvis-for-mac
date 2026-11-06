"""Make voice samples so you can choose Jarvis's voice by ear."""
import numpy as np
from scipy.io import wavfile
from jarvis_core.voice import Voice, SR

SCRIPT = ("Good evening, Sir. All systems are online. "
          "You have three unread messages on WhatsApp, one of them from your mother. "
          "Shall I read it to you, Sir?")

CANDIDATES = {
    "1-george":        {"bm_george": 1.0},
    "2-george-fable":  {"bm_george": 0.6, "bm_fable": 0.4},
    "3-fable":         {"bm_fable": 1.0},
    "4-george-lewis":  {"bm_george": 0.7, "bm_lewis": 0.3},
    "5-lewis":         {"bm_lewis": 1.0},
    "6-daniel":        {"bm_daniel": 1.0},
}


def make_samples(cfg, out_dir=None):
    from pathlib import Path
    out = Path(out_dir or cfg.voice.cache_dir.parent.parent / "samples")
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.wav"):
        f.unlink()
    for name, blend in CANDIDATES.items():
        cfg.voice.blend = blend
        cfg.voice.polish = 1.0
        a = Voice(cfg.voice).render(SCRIPT, use_cache=False)
        wavfile.write(out / f"{name}.wav", SR, (np.clip(a, -1, 1) * 32767).astype(np.int16))
        print("wrote", out / f"{name}.wav")
    cfg.voice.blend = CANDIDATES["2-george-fable"]  # comparison sample; Jarvis himself uses config.py
    cfg.voice.polish = 0.0
    a = Voice(cfg.voice).render(SCRIPT, use_cache=False)
    wavfile.write(out / "7-george-fable-without-finish.wav", SR, (np.clip(a, -1, 1) * 32767).astype(np.int16))
    print("wrote", out / "7-george-fable-without-finish.wav")
