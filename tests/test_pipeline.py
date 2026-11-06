"""End-to-end: scripted conversations through a simulated microphone.

A different voice (American, am_michael) plays you, so the test doesn't depend on
Jarvis's own voice. Jarvis's speech is not played; we check what it decided to say.
"""
import sys, time; sys.path.insert(0, '.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core import safety
from jarvis_core.config import Config
from jarvis_core.assistant import Assistant
from tests.fakes import install
install()

cfg = Config()
safety.init_log(cfg.safety.log_file.parent / 'test-actions.log')
safety.enforce_offline()
k = Kokoro(str(cfg.voice.model), str(cfg.voice.voices))

def speech(text, voice='am_michael'):
    a, _ = k.create(text, voice=voice, lang='en-us')
    return signal.resample_poly(a, 2, 3)

def silence(s): return np.zeros(int(16000 * s))

class ScriptMic:
    """Feeds a script frame by frame. Flushing does nothing: the script is the future."""
    def __init__(self, parts):
        x = np.concatenate(parts + [silence(2)])
        x = x + np.random.default_rng(1).standard_normal(len(x)) * 0.002
        self.x = (np.clip(x, -1, 1) * 32767).astype(np.int16); self.i = 0; self.finished = False
    def read(self, timeout=1.0):
        if self.i + 1280 > len(self.x): self.finished = True; return None
        f = self.x[self.i:self.i + 1280]; self.i += 1280; return f
    def flush(self): pass

GREET = []   # scenarios that start with Jarvis's greeting

SCENARIOS = [
  ("after the greeting he listens 4 s (no wake word needed)", [silence(1.0), speech("What time is it?"), silence(5)],
   lambda j: j.transcripts == ["What time is it?"] and any("o'clock" in s or ":" in s for s in j.spoken)),
  ("a pause mid-sentence doesn't cut you off",
   [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Remind me to call mum and"), silence(1.0), speech("buy milk at 6 pm."), silence(6)],
   lambda j: len(j.transcripts) >= 1 and "milk" in j.transcripts[0].lower() and "mum" in j.transcripts[0].lower()),
  ("'that's all' ends the conversation",
   [silence(1), speech("Hey Jarvis, what's the date today?"), silence(1.0), speech("That's all."), silence(1.0), speech("What time is it?"), silence(5)],
   lambda j: j.transcripts[:2] == ["What's the date today?", "That's all."] and len(j.transcripts) == 2),
  ("wake, pause, ask the time", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("What time is it?")],
   lambda j: any("o'clock" in s or ":" in s for s in j.spoken) and j.running),
  ("one breath: date", [silence(1), speech("Hey Jarvis, what's the date today?")],
   lambda j: any(s.startswith("Today is") for s in j.spoken) and len(j.spoken) == 1),
  ("Hey Travis does nothing", [silence(1), speech("Hey Travis."), silence(1), speech("What time is it?")],
   lambda j: j.spoken == [] and j.mic.finished),
  ("go offline, answer no", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Go offline."), silence(1.0), speech("No.")],
   lambda j: j.running and any("Cancelled" in s or "won't" in s for s in j.spoken)),
  ("go offline, say nothing", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Shut down."), silence(10)],
   lambda j: j.running and any("take that as a no" in s for s in j.spoken)),
  ("go offline, answer 'yes, actually no'", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Power down."), silence(1.0), speech("Yes. Actually, no, wait.")],
   lambda j: j.running and [t.lower() for t in j.transcripts[:1]] == ['power down.'] and len(j.transcripts) == 2),
  ("open WhatsApp (instantly, by voice)", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Open WhatsApp.")],
   lambda j: j.actions == [("open_app", {"name": "whatsapp"})]),
  ("brain answer, then a follow-up without the wake word",
   [silence(1), speech("Hey Jarvis, who painted the Mona Lisa?"), silence(1.5), speech("When was he born?"), silence(6)],
   lambda j: len(j.transcripts) == 2 and any("vinci" in s.lower() for s in j.spoken) and any("1452" in s for s in j.spoken)),
  ("go offline, answer yes", [silence(1), speech("Hey Jarvis."), silence(1.2), speech("Go offline."), silence(1.0), speech("Yes, please.")],
   lambda j: not j.running),
]

jarvis = None
passed = 0
for name, parts, check in SCENARIOS:
    mic = ScriptMic(parts)
    if jarvis is None:
        jarvis = Assistant(cfg, mic, speak=False, hud=False, log=lambda *a: None)
    jarvis.mic = mic; jarvis.running = True; jarvis.wake._quiet_frames = 0; jarvis.spoken = []; jarvis.transcripts = []; jarvis.actions = []; jarvis.wake.reset()
    t = time.time(); jarvis.run(greet=name.startswith("after the greeting")); dt = time.time() - t
    ok = bool(check(jarvis)); passed += ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({dt:.1f}s)\n      heard={jarvis.transcripts}\n      said={jarvis.spoken}")
print(f"\n{passed}/{len(SCENARIOS)} scenarios passed")
