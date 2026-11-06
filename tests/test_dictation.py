"""fn dictation, end to end, with a made-up voice and a stand-in helper: no real keyboard, text box or clipboard.

Speech (Kokoro, a voice that isn't yours) goes through the real Whisper and the real clean-up, then to a
stand-in for helpers/JarvisDictate that records whether Jarvis typed it, showed the card, or neither.
Also checks the fn key logic in the real helper (made-up key events only) and that nothing said is logged.
"""
import json, re, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, '.')
import numpy as np
from scipy import signal
from kokoro_onnx import Kokoro
from jarvis_core import safety, settings
from jarvis_core.config import Config
from jarvis_core.assistant import Assistant
from jarvis_core.dictate import Dictation, DictationRequested, Helper
from jarvis_core.polish import Polisher, rules, faithful, as_points, tidy_list, counted
from tests.fakes import install
install()

tmp = Path(tempfile.mkdtemp())
LOG = tmp / "actions.log"
safety.init_log(LOG); safety.enforce_offline()
cfg = Config()
k = Kokoro(str(cfg.voice.model), str(cfg.voice.voices))
results = []

def case(name, ok, detail=""):
    results.append(bool(ok)); print(f"{'PASS' if ok else 'FAIL'}  {name:58s} {detail}")

def you(text, voice="am_michael"):
    a, _ = k.create(text, voice=voice, lang="en-us")
    return signal.resample_poly(a, 2, 3)

def silence(s): return np.zeros(int(16000 * s))

# ---------------------------------------------------------------- Layer A rules
for heard, want in [
    ("um so I I think we should uh go tomorrow", "So I think we should go tomorrow"),
    ("Hi, comma, how are you? Question mark.", "Hi, how are you?"),
    ("Let's meet at 5. Scratch that. Let's meet at 6.", "Let's meet at 6."),
    ("Dear sir new paragraph thank you for the call", "Dear sir\n\nThank you for the call"),
    ("the trial period is over", "The trial period is over"),
    ("it was, like, really good", "It was really good"),
    ("i think i'm late", "I think I'm late"),
]:
    got = rules(heard)[0]
    case(f"rules: {heard[:40]!r}", got == want, f"-> {got!r}")
case("rules: 'as a numbered list' asks for numbers", rules("buy milk call mum as a numbered list")[1]["list"] == "numbered")
case("rules: 'make it points' asks for points", rules("make it points eggs, bread and jam")[1]["list"] == "points")
case("rules: 'first… second…' becomes numbered by itself", rules("first wake up second run")[1]["list"] == "numbered")
case("points without the model", as_points("Eggs, bread and jam") == "• Eggs\n• Bread\n• Jam")

# ---------------------------------------------------------------- lists said out loud (intro line, spoken counting, a broken-off item)
LIST_SAID = "I want to sort the trip out, so I, there are three points that I need to cover. One is the train tickets. Two is the hotel booking is the... Three. And the passports."
got = as_points(rules(LIST_SAID)[0])
lines = got.split("\n")
case("list: the intro line stays above the list, with ':'", lines[0].endswith("need to cover:") and not lines[0][0].isdigit(),
     repr(lines[0]))
case("list: the spoken 'one/two/three' are not written in the items",
     lines[1:] == ["1. The train tickets", "2. The hotel booking", "3. The passports"], repr(lines[1:]))
case("list: 'two sleeping bags' keeps its two", "Two sleeping bags" in tidy_list("• Two sleeping bags\n• One tent\n• A stove"))
case("list: 'I have one brother and two sisters' is not a list", counted("I have one brother and two sisters.") is None)
case("list: 'firstly… secondly… thirdly' counts", counted("A few things. Firstly the budget. Secondly the plan and thirdly the team.") is not None)
case("list: the model's intro-as-item-1 is moved back up",
     tidy_list("1. There are three points I need to cover\n2. One is the tickets\n3. Two, the hotel\n4. Three. The passports")
     == "There are three points I need to cover:\n1. The tickets\n2. The hotel\n3. The passports")

# ---------------------------------------------------------------- the safety net
case("safety net: an answer is refused", not faithful("what is the capital of France", "The capital of France is Paris.", False))
case("safety net: a poem is refused", not faithful("write a poem about the moon", "Silver moon above the night, glowing soft.", False))
case("safety net: dropping half of it is refused",
     not faithful("call the bank about the loan and then pick up the kids from school", "Call the bank.", False))
case("safety net: a grammar fix is fine", faithful("I went to the market and I buyed some apples",
                                                    "I went to the market and bought some apples.", False))
case("safety net: a self-correction may drop words", faithful("meet at 5 actually 6 at the cafe", "Meet at 6 at the cafe", True))

full = Polisher(url=cfg.brain.url)
full.model_edit("warm up", "Notes", timeout=60)
out = full.polish(LIST_SAID, "Notes")
case("list with the model: intro tidied, items clean", out.split("\n")[1:] == ["1. The train tickets", "2. The hotel booking",
     "3. The passports"] and "so I, there" not in out and out.split("\n")[0].endswith(":"), repr(out))
PARA = ("so basically what happened was that I went to the the shop yesterday and um the guy there he told me that the "
        "the item is not available and I have to come back on Monday but Monday I have school so I don't know what to do "
        "and also the price went up by like 200 rupees which is honestly too much for me right now so I think I'll just "
        "order it online from Amazon instead")
out = full.polish(PARA, "Notes")
sents = [x for x in re.split(r"(?<=[.!?])\s+", out) if x]
case("long paragraph: split into sentences, nothing made a list", len(sents) >= 3 and "\n•" not in out and "1." not in out,
     f"{len(sents)} sentences, layer {full.last.get('layer')}")
case("…your words kept, no fancier ones", all(w in out.lower() for w in ("shop", "guy", "monday", "school", "200 rupees", "amazon")))

class Refuser(Polisher):
    def model_edit(self, text, app="", fmt="auto", timeout=None): return "Sure! The capital of France is Paris."
p = Refuser()
out = p.polish("um what is the capital of France and its population", "Notes")
case("a model that answers: your words are typed instead", "Paris" not in out and "capital of France" in out, f"-> {out!r}")
class Down(Polisher):
    def model_edit(self, text, app="", fmt="auto", timeout=None): return None
out = Down().polish("um I'll be there by six o'clock tonight probably", "Notes")
case("model not running: the rules version is typed", out.startswith("I'll be there by six"), f"-> {out!r}")

# ---------------------------------------------------------------- fn key logic in the real helper
from jarvis_core.paths import HELPERS
helper_bin = HELPERS / "JarvisDictate"
if helper_bin.exists():
    lines = subprocess.run([str(helper_bin), "--test-fn"], capture_output=True, text=True, timeout=60).stdout.splitlines()
    seq, cur = {}, None
    for l in lines:
        o = json.loads(l)
        if "case" in o: cur = o["case"]; seq[cur] = []
        elif "fn" in o: seq[cur].append(o["fn"])
    expect = {"hold": ["down", "up"], "tap": ["down", "tap"],
              "double tap, then tap to finish": ["down", "tap", "down", "handsfree", "stop"],
              "fn+F5": ["down", "cancel"], "fn+shift": ["down", "cancel"],
              "hands-free, Esc": ["down", "tap", "down", "handsfree", "cancel"],
              "two slow taps": ["down", "tap", "down", "tap"]}
    for name, want in expect.items():
        case(f"fn key: {name}", seq.get(name) == want, f"{seq.get(name)}")
else:
    case("fn key helper built", False, "helpers/JarvisDictate missing")

# ---------------------------------------------------------------- whole dictations
class Mic:
    aec = True
    realtime = False
    def __init__(self): self.load([silence(0.5)])
    def load(self, parts):
        x = np.concatenate(parts + [silence(0.3)]) + np.random.default_rng(1).standard_normal(
            sum(map(len, parts)) + 4800) * 0.002
        self.x = (np.clip(x, -1, 1) * 32767).astype(np.int16); self.i = 0; self.finished = False
    def read(self, timeout=1.0):
        if self.i >= len(self.x): self.finished = True; return None
        if self.realtime: time.sleep(0.08)        # like a real microphone: one frame every 80 ms
        f = self.x[self.i:self.i + 1280]; self.i += 1280
        return np.pad(f, (0, 1280 - len(f)))
    def flush(self): pass
    def set_volume(self, v): pass

class FakeHelper(Helper):
    """Stands in for helpers/JarvisDictate. `where` is what the text box check says."""
    def __init__(self):
        super().__init__(path=None); self.sent = []; self.released = 0.0; self.typed_at = 0.0; self.where = {"editable": True, "secure": False, "app": "Notes"}
    @property
    def alive(self): return True
    def send(self, **msg): self.sent.append(msg)
    def request(self, key, timeout=1.5, **msg):
        self.sent.append(msg)
        if msg.get("cmd") == "target" and not self.released: self.released = time.monotonic()
        if msg.get("cmd") in ("paste", "card"): self.typed_at = time.monotonic()
        return {"target": self.where, "pasted": True, "card": True, "status": {"tap": True}}.get(key)
    def push(self, *events):
        for e in events:
            if e in ("down", "handsfree"): self._down.set()
            self.events.put(e)
    def did(self, cmd): return [m for m in self.sent if m.get("cmd") == cmd]
    def pills(self): return [m.get("state") for m in self.did("pill")]

mic = Mic()
j = Assistant(cfg, mic, speak=False, hud=False, log=lambda *a: None)
h = FakeHelper()
j.dictation = Dictation(j, helper=h, polisher=Polisher(url=cfg.brain.url))
j.dictation.polisher.model_edit("warm up", "Notes", timeout=60)   # load the editing model first
heard_pieces = []
_hear = j.dictation._hear
j.dictation._hear = lambda frames: (heard_pieces.append(len(frames)), _hear(frames))[1]

def dictate(parts, *events, where=None, mode="hold", realtime=False):
    h.sent.clear(); heard_pieces.clear(); mic.load(parts); mic.realtime = realtime; h.released = 0.0
    h.where = where or {"editable": True, "secure": False, "app": "Notes"}
    h.push(*events)
    t = time.monotonic()
    j.dictation.run(mode, [])
    return time.monotonic() - t

SAID = "Um, I think we should meet at five, actually six, at the library tomorrow."
dictate([silence(0.3), you(SAID), silence(0.4)], realtime=True)
after = h.typed_at - h.released
typed = h.did("paste")
text = typed[0]["text"] if typed else ""
case("held fn: typed into the text box", typed and "6" in text or "six" in text.lower(), f"-> {text!r}")
case("…cleaned: no 'um', and the correction kept", typed and "um" not in text.lower().split() and "five" not in text.lower())
case("…pill went listening → writing → done", h.pills()[:1] == ["listening"] and "working" in h.pills() and "done" in h.pills(),
     f"{h.pills()}")
case("…quick: typed within 1.5 s of letting go", after < 1.5, f"{after:.2f}s for {len(mic.x) / 16000:.1f}s of speech")

dictate([silence(0.3), you("Please send me the notes from today's class."), silence(0.4)],
        where={"editable": False, "secure": False, "app": "Finder"})
case("no text box: the card is shown (and copied)", h.did("card") and not h.did("paste"),
     f"-> {(h.did('card') or [{}])[0].get('text')!r}")

dictate([silence(0.3), you("My password is hunter two."), silence(0.4)], where={"editable": True, "secure": True, "app": "Safari"})
case("password box: never typed into", not h.did("paste") and h.did("card") and "error" in h.pills(), f"{h.pills()}")

dictate([silence(0.3), you("Cancel this one please."), silence(0.4)], "cancel")
case("fn + another key: forgotten, nothing typed", not h.did("paste") and not h.did("card"))
dictate([silence(0.3), you("Just a tap."), silence(0.4)], "tap")
case("a quick tap of fn: nothing typed", not h.did("paste") and not h.did("card"))
dictate([silence(1.5)])
case("silence: 'Didn't catch that', nothing typed", not h.did("paste") and "nothing" in h.pills(), f"{h.pills()}")

LONG = [silence(0.3), you("For the trip we need to pack a few things."), silence(1.0),
        you("The tent, the stove, two sleeping bags and the first aid kit."), silence(1.0),
        you("And don't forget to charge the torch before we leave on Saturday morning."), silence(0.4)]
dictate(LONG, "handsfree", mode="hold", realtime=True)
after = h.typed_at - h.released
text = (h.did("paste") or [{}])[0].get("text", "")
case("hands-free, long: written piece by piece at the pauses", len(heard_pieces) >= 2, f"{len(heard_pieces)} pieces")
case("…and all of it typed", all(w in text.lower() for w in ("tent", "stove", "torch", "saturday")), f"-> {text!r}")
case("…the pill said hands-free", "handsfree" in h.pills())
case("…long, still quick after letting go", after < 2.0, f"{after:.2f}s for {len(mic.x) / 16000:.1f}s of speech")

settings.save(dictation_polish="off")
dictate([silence(0.3), you("Um, testing one two three."), silence(0.4)])
raw = (h.did("paste") or [{}])[0].get("text", "")
case("'type exactly what I say': no clean-up", raw and j.dictation.polisher.last.get("layer") == "off", f"-> {raw!r}")
settings.save(dictation_polish="full")

case("paste that again: types the last one again", j.dictation.paste_again() and h.did("paste"))

# fn pressed while Jarvis is listening: he gives way at once
h.sent.clear(); mic.load([silence(3)])
h.push("down")
try:
    j.listen(timeout=3); gave_way = False
except DictationRequested:
    gave_way = True
case("fn while he's listening: he stops and gives way", gave_way)
h.next_event()                                     # what the run loop would do with it

# The run loop: fn pressed while idle → a dictation, then back to listening
mic.load([silence(0.3), you("Hello from the run loop."), silence(0.4)])
h.sent.clear(); h.push("down")
j.run(greet=False)
case("run loop: fn while idle starts a dictation", h.did("paste"), f"-> {(h.did('paste') or [{}])[0].get('text')!r}")

# Voice commands
for said, check in [("turn off dictation", lambda: settings.load()["dictation"] is False),
                    ("turn on dictation", lambda: settings.load()["dictation"] is True),
                    ("polish dictation lightly", lambda: settings.load()["dictation_polish"] == "light"),
                    ("type exactly what I say", lambda: settings.load()["dictation_polish"] == "off"),
                    ("polish dictation fully", lambda: settings.load()["dictation_polish"] == "full")]:
    j.spoken.clear()
    handled = j.fast_path(said)
    case(f"voice: {said!r}", handled and check(), " | ".join(j.spoken)[:90])

log = LOG.read_text()
case("the log never holds what was dictated",
     not any(w in log.lower() for w in ("library", "tent", "hunter", "notes from today", "testing", "hello from")))
case("…but does record that dictations happened", '"dictation"' in log)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
