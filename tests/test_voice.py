"""Voice training and 'only my voice'. A synthetic voice plays you through a simulated microphone."""
import sys, time; sys.path.insert(0, '.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core import safety, settings
from jarvis_core.config import Config
from jarvis_core.assistant import Assistant
from tests.fakes import install
install()
safety.init_log(Config().safety.log_file.parent / "test-actions.log"); safety.enforce_offline()
cfg = Config()
k = Kokoro(str(cfg.voice.model), str(cfg.voice.voices))
def speech(t, v="am_michael", sp=1.0):
    a, _ = k.create(t, voice=v, speed=sp, lang="en-us"); return signal.resample_poly(a, 2, 3)
def silence(s): return np.zeros(int(16000 * s))

class ScriptMic:
    def __init__(self, parts, gain=None):
        x = np.concatenate(parts + [silence(2)]); x = x + np.random.default_rng(3).standard_normal(len(x)) * 0.002
        self.x = (np.clip(x, -1, 1) * 32767).astype(np.int16); self.i = 0; self.finished = False
    def read(self, timeout=1.0):
        if self.i + 1280 > len(self.x): self.finished = True; return None
        f = self.x[self.i:self.i + 1280]; self.i += 1280; return f
    def flush(self): pass

results = []
def case(name, ok, detail=""):
    results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name:60s} {detail}")

# 1. Training, for real (real voice model, simulated microphone)
parts = [silence(0.5)]
for sentence in Assistant.TRAINING:
    parts += [speech(sentence), silence(1.4)]
mic = ScriptMic(parts)
j = Assistant(cfg, mic, speak=False, hud=False, log=lambda *a: None)
t = time.time(); j.train_voice()
case("voice training learns your voice from 8 sentences", j.voiceprint.trained, f"({time.time() - t:.0f}s)")
data = np.load(j.voiceprint.file)
case("only numbers are kept, no recordings", set(data.files) <= {"me", "adjust", "sentences", "when", "model"}
     and data["me"].size == 192, str(data.files))
ok, score = j.voiceprint.check(speech("Hey Jarvis, open WhatsApp and check my messages.").astype(np.float32))
case("a new sentence in your voice is accepted", ok is True, f"similarity {score:.2f}")

# 2. The gate, with a stand-in check: quiet audio plays 'someone else'
class FakeVP:
    trained = True
    def check(self, audio):
        loud = float(np.max(np.abs(audio)))
        return (loud > 0.25), (0.7 if loud > 0.25 else 0.1)
    def forget(self): pass
j.voiceprint = FakeVP()
settings.save(only_my_voice=True)
other = lambda t: speech(t, "bm_george") * 0.15           # someone else, further away
you = lambda t: speech(t) * 0.9

j.mic = ScriptMic([silence(1), other("Hey Jarvis, what time is it?"), silence(3)])
j.spoken.clear(); j.transcripts.clear(); j.wake._quiet_frames = 0; j.wake.reset(); j.running = True
j.run(greet=False)
case("someone else saying 'Hey Jarvis' is ignored", j.spoken == [], f"said={j.spoken}")

j.mic = ScriptMic([silence(0.5), other("Play some loud music right now."), silence(0.6), you("What time is it?"), silence(3)])
j.spoken.clear(); j.transcripts.clear(); j.actions.clear()
heard = j.listen(timeout=6)
case("someone talking in the background is skipped; your words are heard", heard.lower().startswith("what time"),
     f"heard={heard!r}")

settings.save(only_my_voice=False)
j.mic = ScriptMic([silence(0.5), other("What time is it?"), silence(3)])
heard = j.listen(timeout=4)
case("'listen to everyone' turns the check off", "time" in heard.lower(), f"heard={heard!r}")
settings.save(only_my_voice=True)

j.handle("Listen to everyone"); a = settings.load()["only_my_voice"]
j.handle("Only listen to me"); b = settings.load()["only_my_voice"]
case("voice commands switch it off and on", a is False and b is True)
print(f"\n{sum(results)}/{len(results)} passed")
