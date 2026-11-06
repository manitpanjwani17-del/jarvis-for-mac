"""Talking over Jarvis. A simulated echo-cancelled microphone plays your voice (a different speaker)
in real time while he talks; his speech goes to a stand-in speaker that only keeps time."""
import sys, time, threading; sys.path.insert(0, '.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core import safety, skills, messaging
from jarvis_core.config import Config
from jarvis_core.assistant import Assistant, Interrupted
from jarvis_core.skills import Result
from tests.fakes import install, FAKE
install()
safety.init_log(Config().safety.log_file.parent / "test-actions.log"); safety.enforce_offline()
cfg = Config()
k = Kokoro(str(cfg.voice.model), str(cfg.voice.voices))

def you(text, voice="am_michael"):
    a, _ = k.create(text, voice=voice, lang="en-us")
    return signal.resample_poly(a, 2, 3)

def silence(s): return np.zeros(int(16000 * s))

class EchoFreeMic:
    """Frames arrive in real time from the moment Jarvis starts listening, like a real microphone."""
    aec = True
    def __init__(self): self.load([silence(1)])
    def load(self, parts):
        x = np.concatenate(parts + [silence(3)]) + np.random.default_rng(1).standard_normal(sum(map(len, parts)) + 48000) * 0.002
        self.x = (np.clip(x, -1, 1) * 32767).astype(np.int16); self.i = 0; self.t0 = None; self.finished = False
        self.volumes = []
    def read(self, timeout=1.0):
        if self.t0 is None: self.t0 = time.monotonic()
        due = self.t0 + (self.i / 1280) * 0.08
        wait = due - time.monotonic()
        if wait > 0: time.sleep(min(wait, timeout))
        if self.i + 1280 > len(self.x): self.finished = True; return None
        f = self.x[self.i:self.i + 1280]; self.i += 1280; return f
    def flush(self): pass
    def set_volume(self, v): self.volumes.append(v)

class FakeSpeaker:
    alive = True
    def __init__(self): self.played = 0.0; self.cut = False
    def play(self, audio, on_level=lambda l: None, stop=None):
        dur = len(audio) / 24000; t = time.monotonic()
        while time.monotonic() - t < dur:
            if (stop is not None and stop.is_set()) or self.cut:
                self.cut = False; self.played += time.monotonic() - t; return False
            time.sleep(0.02)
        self.played += dur; return True
    def stop_speaking(self): self.cut = True
    def set_volume(self, v): pass

mic, spk = EchoFreeMic(), FakeSpeaker()
j = Assistant(cfg, mic, speak=True, hud=False, log=lambda *a: None)
j.voice.output = spk
LONG = ["The Mona Lisa was painted by Leonardo da Vinci in the early sixteenth century.",
        "It hangs in the Louvre in Paris, behind bulletproof glass, and draws millions of visitors every year.",
        "Its fame grew enormously after it was stolen in nineteen eleven and recovered two years later."]

def speak_over(parts, lines=LONG):
    mic.load(parts); spk.played = 0.0
    try:
        j.say_stream(iter(lines)); return None
    except Interrupted as e:
        return e.text

calls = []
FAKE["volume"] = lambda **a: (calls.append(("volume", a)), Result("Volume set, Sir."))[1]
skills.REGISTRY["volume"].fn = FAKE["volume"]

results = []
def case(name, ok, detail=""):
    results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name:52s} {detail}")

heard = speak_over([silence(2.0), you("Stop."), silence(2)])
case("'stop' over him: he stops early", heard is not None and "stop" in heard.lower() and spk.played < 8,
     f"heard={heard!r} played={spk.played:.1f}s")
case("…and 'stop' means silence (nothing to handle)", j.on_interruption(heard or "") is None)

echo = signal.resample_poly(np.concatenate([j.voice.render(l) for l in LONG[:1]]), 2, 3) * 0.35
heard = speak_over([silence(0.5), echo, silence(1)], LONG[:2])
case("his own leftover echo doesn't interrupt him", heard is None, f"heard={heard!r}")

heard = speak_over([silence(2.0), you("What time is it?"), silence(2)])
nxt = j.on_interruption(heard or "")
case("a new question over him becomes the next request", nxt is not None and "time" in nxt.lower(), f"next={nxt!r}")

# Answering "yes" while he's still reading the message back
messaging.CONTACTS_LOOKUP = lambda w: [{"name": "Priya Shah", "phones": [{"label": "mobile", "number": "+91 98200 55555"}]}] \
    if w and "priya".startswith(w[0]) else []
j._no_whatsapp = True; j._screen_locked = lambda: False
sent = []
j._sender = lambda app, target, text: sent.append((target.name, text))
mic.load([silence(3.5), you("Yes, send it."), silence(4)])
j.listen = lambda timeout=None: ""      # nothing after he finishes: only the early answer can send
r = j.send_message_flow("Priya", "I'm on my way and will be there in ten minutes")
case("'yes' while he reads the message back counts", sent == [("Priya Shah", "I'm on my way and will be there in ten minutes")],
     f"sent={sent}")
del j.listen

# Undo
calls.clear(); j.undo_stack.clear()
j.run_skill("volume", {"action": "set", "level": 30})
msg = j.undo_last()
case("'undo' after a volume change puts it back", any(c[1].get("action") == "set" and c[1].get("level") != 30 for c in calls[1:])
     and "Volume back" in msg, f"{msg!r} {calls}")
j.undo_stack.append(("A sent message can't be unsent by me, Sir. You can delete it in WhatsApp yourself.", None))
case("'undo' after a sent message is honest", "can't be unsent" in j.undo_last())
case("'undo' with nothing to undo", "nothing" in j.undo_last())
print(f"\n{sum(results)}/{len(results)} passed")
