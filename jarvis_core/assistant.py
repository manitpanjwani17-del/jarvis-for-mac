"""Jarvis's main loop: wait for "Hey Jarvis", listen, understand, act, answer.

How a request is handled
1. Common commands (time, volume, pause, open an app, timers…) are recognised
   instantly by simple rules, without the AI model.
2. Everything else goes to the brain (a local AI model). It answers in conversation
   or chooses skills from skills.py.
3. A skill marked "confirm" runs only after Jarvis asks and hears a clear yes.
4. Jarvis starts speaking as soon as the first sentence of his answer is ready.
5. A conversation: after every reply he listens for a few more seconds, so you can carry on
   without "Hey Jarvis". "That's all" (or silence) ends it.
6. The brain may take several steps (read a group, then set a reminder from what it says).
   Anything read from messages, the clipboard or the web can't make him send or do anything
   risky on its own: such a step is dropped unless your own words asked for it.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from typing import Iterable

import numpy as np

from . import messaging, persona, safety, settings, skills, wa_reader
from .brain import Brain, BrainUnavailable, pseudo_tool_calls, sentences
from .dictate import Dictation, DictationRequested
from .config import Config
from .ears import _NOT_JARVIS, Hearing, SpeechDetector, WakeWord, record_command
from .hud import HUD
from .skills import TIMERS, Result
from .timeparse import duration_seconds, parse_when
from .voice import Voice, chime


_TIME_PHRASE = re.compile(
    r"\b(in (?:\d+|an?|half an|a couple of|one|two|three|five|ten|fifteen|twenty|thirty|forty five)\b"
    r"|at \d|at noon|at midnight|tomorrow|tonight|this (?:morning|afternoon|evening)"
    r"|(?:on |next )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))")


CAPABILITIES = ("Quite a lot, Sir. Hold the fn key and I'll type what you say, cleaned up, wherever you're typing. I read and send WhatsApp messages, groups included, answer from the web, check "
                "the weather and the news, look at or read your screen when you ask, open apps, folders and websites, "
                "play your music in Apple Music, control "
                "volume, set timers, reminders and calendar events, read your calendar, find files, take notes, "
                "work out sums, check the battery, take screenshots, switch Wi-Fi and dark mode, run your "
                "Shortcuts, send WhatsApp messages and iMessages, and remember what you tell me. Anything risky, "
                "and every message, I ask first.")

_MATHS = re.compile(r"\d|\b(plus|minus|times|multiplied|divided|percent|per cent|square|root|half|double|twice|"
                    r"sum|total|add|subtract|average|how much|how many|convert|split|tip|cube|power|squared)\b", re.I)

_DAYCOUNT = re.compile(r"\b(how many|how long|days?|weeks?|months?|until|till|from now|from today|ago|"
                       r"what date|which day|what day)\b", re.I)

_PROMISE = re.compile(r"^(playing|opening|searching|setting|adding|starting|launching|closing|turning)\b|"
                      r"\b(I'll|I will|I shall|let me|I'm going to|I am going to|I'll need to|I need to)\s+(open|search|look|find|set|play|"
                      r"add|create|make|check|put|turn|start|remind|close|quit|bring|pull|launch|remember|note|save|copy|take|"
                      r"send|message|text|tell|whatsapp)", re.I)


_MESSAGE = re.compile(
    r"^(?:please )?(?:send |write )?(?:an? )?(?:whatsapp|whats app|imessage|i message|message|msg|text)"
    r"(?: message)?(?: to)? (?P<to>[a-z0-9+][a-z0-9' +\-]{0,40}?)(?: on (?:whatsapp|imessage))?"
    r"(?: saying| that says| to say| that| telling (?:him|her|them)(?: that)?)[,:]? (?P<body>.+)$")


_STOP = {"the", "and", "you", "your", "are", "for", "that", "this", "with", "have", "has", "was", "will", "can",
         "what", "when", "how", "not", "but", "all", "just", "its", "it's", "i'm", "i'll", "she", "he", "they"}


_COMMAND_WORDS = {"send", "message", "messages", "whatsapp", "whats", "app", "text", "tell", "ask", "imessage", "write",
                  "saying", "say", "that", "a", "an", "to", "the", "my", "please", "on", "jarvis", "hey", "group", "msg",
                  "reply", "sir"}


def words_from(message: str, request: str, to: str = "") -> bool:
    """Do the message's real words come from what you said? ("Are you free tonight?" from
    "ask Priya if she's free tonight" yes; "Sir wishes to send a message" from "send a message to
    Priya", no: the command and the person's name don't count as words of the message.)"""
    recipient = set(re.findall(r"[a-z0-9']+", to.lower()))
    said = set(re.findall(r"[a-z0-9']+", request.lower())) - _COMMAND_WORDS - recipient
    if not said:
        return False            # you said who, but not what
    words = re.findall(r"[a-z0-9']+", message.lower())
    content = [w for w in words if len(w) > 2 and w not in _STOP and w not in recipient]
    if not content:
        return any(w in said for w in words)
    return sum(w in said for w in content) / len(content) >= 0.5


def redact_command(text: str) -> str:
    """The log keeps who you messaged, never the words of a message."""
    t = text.lower().strip(" .!?")
    m = _MESSAGE.match(t)
    if m:
        return f"(a message to {m.group('to').strip()})"
    if re.match(r"^(please )?(tell|ask|message|text|whatsapp|whats app|imessage|send|reply|write to)\b", t):
        return "(a message request)"
    return text


def tidy_message(text: str) -> str:
    t = (text or "").strip().strip("\"“”'").strip()
    t = re.sub(r"\s+", " ", t)
    return t[:1].upper() + t[1:] if t else ""


def edited_message(answer: str) -> str | None:
    """'No, change it to running late' -> 'running late'. None if the answer isn't an edit."""
    m = re.match(r"^\W*(?:no\W+|actually\W+)?(?:change it to|change that to|change the message to|make it|"
                 r"replace it with|instead say|say instead|it should say|write)\W+(.+)$", (answer or "").strip(), re.I)
    return m.group(1).strip() if m else None


class Interrupted(Exception):
    """You spoke while Jarvis was talking. `text` is what you said."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


_ANSWER_WORDS = {"yes", "yeah", "yep", "no", "nope", "stop", "cancel", "wait", "undo", "quiet", "enough", "okay", "ok",
                 "jarvis", "don't", "sure", "send", "hold", "shut", "pause"}

_HUSH = re.compile(r"^(?:jarvis\W+)?(stop|stop it|stop talking|quiet|be quiet|keep quiet|shut up|enough|that'?s enough|"
                   r"silence|hush|okay stop|ok stop|please stop|wait|hold on|stop stop|shh+|okay okay|ok ok|got it)$")
_UNDO = re.compile(r"^(?:no\W+)?(undo|undo that|undo it|cancel|cancel that|cancel it|don'?t do that|reverse that|"
                   r"put it back|take that back|change it back|no don'?t|no no|scrap that)$")


# Skills whose results come from other people or the web: never a reason to act on their own.
UNTRUSTED = {"read_messages", "whatsapp_unread", "read_clipboard", "look_up", "news", "find_files", "list_reminders",
             "calendar", "briefing", "list_shortcuts", "read_screen_text", "look_at_screen"}
# Jarvis looks at the screen only when your own words are about it.
SCREEN_SKILLS = {"read_screen_text", "look_at_screen"}
_SCREEN_WORDS = re.compile(r"\b(screen|window|page|this|that|these|error|display|look|see|showing|shown|reading|read)\b", re.I)
# Words that show you yourself asked for a message to be sent or something risky to be done.
_ASKED_TO_ACT = re.compile(r"\b(send|reply|respond|message|text|tell|ask|whatsapp|imessage|forward|quit|close|restart|"
                           r"shut|sleep|run|power|go offline)\b", re.I)
_DANGLING = {"and", "or", "but", "to", "the", "a", "an", "of", "for", "with", "my", "at", "in", "on", "is", "that",
             "saying", "because", "so", "then", "if", "about", "from", "your", "his", "her", "their", "um", "uh"}
_END = re.compile(r"^(that'?s all|that is all|that'?ll be all|nothing|nothing else|no thanks|no thank you|no|nope|"
                  r"bye|goodbye jarvis|ok(ay)? thanks?|thanks jarvis|thank you jarvis|i'?m done|all good)$")


class Assistant:
    def __init__(self, cfg: Config, mic, speak: bool = True, hud: bool = True, log=print, brain: Brain | None = None):
        self.cfg = cfg
        self.mic = mic
        self.speak_enabled = speak
        self.log = log
        cfg.hud.enabled = cfg.hud.enabled and hud
        prefs = settings.load()
        if prefs.get("address"):
            cfg.name_for_user = prefs["address"]
        if prefs.get("country_code"):
            cfg.messaging.country_code = str(prefs["country_code"]).lstrip("+")
        self.hud = HUD(cfg.hud)
        self.voice = Voice(cfg.voice, on_level=self.hud.level)
        self.wake = WakeWord(cfg)
        self.detector = SpeechDetector(cfg)
        self.hearing = Hearing(cfg)
        self.brain = brain or Brain(cfg.brain, user=cfg.name_for_user)
        try:
            from .voiceprint import VoicePrint
            self.voiceprint = VoicePrint()
        except Exception as e:          # the model missing must never stop Jarvis
            safety.log_error("loading voice recognition", e)
            self.voiceprint = None
        self._vocab_at = 0.0
        skills.BRAIN = self.brain
        self.running = True
        self.follow_up_ok = False
        self.transcripts: list[str] = []   # for tests
        self.spoken: list[str] = []        # for tests
        self.actions: list[tuple[str, dict]] = []  # for tests: skills actually run
        self.last_answer = ""                      # for "copy that"
        self._battery_state: dict = {}
        self.rate = messaging.RateLimit(cfg.messaging.per_minute, cfg.messaging.per_hour)
        self.sent_messages: list[dict] = []   # for tests: what was actually sent
        self.end_conversation = False
        self._recent_skills: list[str] = []
        self.undo_stack: list[tuple] = []
        self._wa_last_pk: int | None = None   # newest WhatsApp message seen, for reading new ones aloud
        self._wa_checked = 0.0
        speed = settings.load().get("voice_speed")
        if speed:
            self.voice.cfg.speed = float(speed)
        self._battery_checked = 0.0
        self.dictation: Dictation | None = None     # fn dictation; started by run()

    def _fn_pressed(self) -> bool:
        """fn went down for dictation while Jarvis is busy talking or listening."""
        return self.dictation is not None and self.dictation.requested()

    # ---------- speaking ----------

    def say(self, text: str, state: str = "speaking", subtext: str | None = None, interruptible: bool = True):
        """Speak one line, showing it on the HUD."""
        return self.say_stream([text], state=state, subtext=subtext, interruptible=interruptible)

    def say_stream(self, lines: Iterable[str], state: str = "speaking", subtext: str | None = None,
                   interruptible: bool = True) -> str:
        """Speak sentences as they arrive, making the next one while the current one plays.

        Saying "Hey Jarvis" while he talks cuts him off. Returns everything he said.
        """
        said: list[str] = []
        if isinstance(lines, list) and len(lines) == 1 and len(lines[0]) > 160:
            lines = list(sentences(iter([lines[0]])))     # long text: speak it piece by piece
        lines = [persona.addressed(x) for x in lines] if isinstance(lines, list) else (persona.addressed(x) for x in lines)
        if not self.speak_enabled:
            for line in lines:
                said.append(line)
                self.spoken.append(line)
                self.log(f"Jarvis: {line}")
                self.hud.show(state, text=" ".join(said)[-160:], subtext=subtext or "")
            return " ".join(said)

        audio_q: queue.Queue = queue.Queue(maxsize=3)
        error: list[Exception] = []

        def produce():
            try:
                for line in lines:
                    audio_q.put((line, self.voice.render(line, use_cache=len(line) < 80)))
            except Exception as e:   # the brain may fail mid-answer
                error.append(e)
            finally:
                audio_q.put(None)

        def play():
            while True:
                item = audio_q.get()
                if item is None:
                    break
                line, audio = item
                if self.voice._stop.is_set():
                    continue          # interrupted: drain what's left without playing it
                said.append(line)
                self.spoken.append(line)
                self.log(f"Jarvis: {line}")
                self.hud.show(state, text=" ".join(said)[-160:], subtext=subtext or "")
                self.voice.play(audio)
                if self.voice._stop.is_set():
                    self.voice._stop.set()   # keep the stop for the remaining lines

        self.voice._stop.clear()
        producer = threading.Thread(target=produce, daemon=True)
        player = threading.Thread(target=play, daemon=True)
        producer.start()
        player.start()
        # While he speaks he keeps listening. "Hey Jarvis" always cuts him off (with a higher bar, so
        # his own voice can't). With echo cancellation on, simply talking over him does too.
        barge = bool(getattr(self.mic, "aec", False)) and self.cfg.listen.barge_in and interruptible
        run, recent, heard, fn = 0, [], None, False
        self.detector.reset()
        while player.is_alive():
            if self._fn_pressed():
                self.voice.stop()          # fn: stop talking, you're dictating
                fn = True
                break
            frame = self.mic.read(timeout=0.1)
            if frame is None or not interruptible:
                continue
            score = float(self.wake.model.predict(frame)[self.wake.name])
            if score >= max(0.8, self.cfg.wake.threshold):
                self.voice.stop()
                safety.log("interrupted", by="wake word")
                break
            if not barge:
                continue
            recent = (recent + [frame])[-10:]
            run = run + 1 if self.detector.prob(frame) >= self.cfg.listen.barge_threshold else 0
            if run >= self.cfg.listen.barge_frames:
                heard = self._check_interruption(recent, " ".join(said[-2:]))
                run, recent = 0, []
                if heard is not None:
                    break
        player.join()
        if fn:
            self._after_speaking()
            raise DictationRequested()
        if heard is not None:
            # Don't wait for the rest of the answer to be made: you've moved on.
            self._after_speaking()
            safety.log("interrupted", by="voice")
            raise Interrupted(heard)
        producer.join(timeout=60)
        self._after_speaking()
        if error:
            raise error[0]
        return " ".join(said)

    def _check_interruption(self, start: list, current_line: str) -> str | None:
        """Someone seems to be talking over him. Lower his voice, listen a moment, and decide:
        your voice (stop and hear you out) or a leftover echo of his own (carry on)."""
        mic = self.mic
        mic.set_volume(0.25)
        frames = list(start)
        t_end = time.monotonic() + self.cfg.listen.barge_check_s
        while time.monotonic() < t_end:
            f = mic.read(timeout=0.1)
            if f is not None:
                frames.append(f)
        audio = np.concatenate(frames).astype(np.float32) / 32768
        try:
            segs, _ = self.hearing.checker.transcribe(audio, language="en", beam_size=1, without_timestamps=True,
                                                      condition_on_previous_text=False)
            quick = " ".join(x.text for x in segs).strip()
        except Exception:
            quick = ""
        if not self._is_you(audio, "talking over me"):
            mic.set_volume(1.0)          # someone else in the room: carry on
            return None
        words = re.findall(r"[a-z0-9']+", quick.lower())
        echo = set(re.findall(r"[a-z0-9']+", current_line.lower()))
        # It's you if you used an answer word he isn't saying himself ("yes", "stop"…), or if most of
        # the words aren't his. Otherwise it's leftover echo of his own sentences.
        answer_word = any(w in _ANSWER_WORDS and w not in echo for w in words)
        if not words or (not answer_word and echo and sum(w in echo for w in words) / len(words) >= 0.6):
            mic.set_volume(1.0)          # his own voice, or nothing: carry on
            return None
        self.voice.stop()                # it's you: stop, and hear the rest of what you're saying
        queued = list(frames)

        def more():
            return queued.pop(0) if queued else mic.read(timeout=1.0)
        old = self.cfg.listen.start_timeout_s
        self.cfg.listen.start_timeout_s = 1.0
        try:
            rest = record_command(more, self.cfg, self.detector, on_level=self.hud.level)
        finally:
            self.cfg.listen.start_timeout_s = old
        mic.set_volume(1.0)
        if rest is None:
            return quick
        text = self.hearing.transcribe(rest) or quick
        text = re.sub(r"^\W*(hey\W+)?(jarvis|javis|jervis|jarvas)\b\W*", "", text, flags=re.I).strip()
        self.transcripts.append(text)
        self.log(f"  (you interrupted: {text})")
        return text or quick

    def chime(self, kind: str):
        if self.speak_enabled:
            self.voice.play(chime(kind))

    def _after_speaking(self):
        # Drop audio captured while Jarvis was talking, so he never hears himself.
        self.mic.flush()
        self.wake.reset()

    # ---------- listening ----------

    def listen(self, timeout: float | None = None) -> str:
        old = self.cfg.listen.start_timeout_s
        if timeout is not None:
            self.cfg.listen.start_timeout_s = timeout
        try:
            return self._listen_from(lambda: self.mic.read(timeout=1.0))
        finally:
            self.cfg.listen.start_timeout_s = old

    def _still_talking(self, audio: np.ndarray) -> bool:
        """At a pause: did you stop mid-sentence ("…and", "tell Mum that")? A quick look at the last words."""
        try:
            segs, _ = self.hearing.checker.transcribe(audio[-16000 * 4:], language="en", beam_size=1,
                                                      without_timestamps=True, condition_on_previous_text=False)
            tail = " ".join(s.text for s in segs).strip().lower()
        except Exception:
            return False
        if not tail:
            return False
        last = re.findall(r"[a-z']+", tail)[-1:] or [""]
        return tail.endswith(",") or last[0] in _DANGLING

    def _is_you(self, audio: np.ndarray, where: str) -> bool:
        """Your voice, or someone else's? Always yes until your voice is trained, or if you said
        'listen to everyone'. Too little speech to tell counts as yes."""
        vp = self.voiceprint
        if vp is None or not vp.trained or not settings.load().get("only_my_voice", True):
            return True
        ok, score = vp.check(audio)
        if ok is False:
            safety.log("other_voice_ignored", where=where, score=round(score or 0, 2))
            self.log(f"  (not your voice, ignored: {where}, similarity {score:.2f})")
            return False
        return True

    def _only_you(self, audio: np.ndarray) -> np.ndarray | None:
        """What was heard may mix you with someone else ("…play loud music. What time is it?"). Split it at
        the pauses, keep only the parts in your voice, and return them (None if none were yours)."""
        vp = self.voiceprint
        if vp is None or not vp.trained or not settings.load().get("only_my_voice", True):
            return audio
        parts = split_at_pauses(audio)
        if len(parts) <= 1:
            return audio if self._is_you(audio, "command") else None
        kept, dropped = [], 0
        for p in parts:
            ok, score = vp.check(p)
            if ok is False:
                dropped += 1
            else:
                kept.append(p)          # yours, or too short to tell (kept with the rest)
        if dropped:
            safety.log("other_voice_ignored", where="part of a command", parts=dropped)
            self.log(f"  (left out {dropped} part(s) in someone else's voice)")
        if not kept or all(len(p) < 16000 * 0.45 for p in kept):
            return None
        gap = np.zeros(int(16000 * 0.25), dtype=np.float32)
        return np.concatenate([x for p in kept for x in (p, gap)])

    def _refresh_vocabulary(self):
        """Names Jarvis should expect to hear (your people, nicknames, groups), so he spells them right."""
        if time.monotonic() - self._vocab_at < 300:
            return
        self._vocab_at = time.monotonic()
        names = [p["name"] for p in messaging.load_people().values()] + list(messaging.load_aliases().keys())
        try:     # artists in your Music library (only if Music is already open: never launches it)
            if skills.app_running("Music"):
                arts = skills.osa('tell application "Music" to get artist of every track of library playlist 1', timeout=8)
                names += [a.strip() for a in arts.split(",") if a.strip()][:60]
        except Exception:
            pass
        names += POPULAR_ARTISTS
        try:
            wa = skills.whatsapp_or_none()
            if wa:
                names += [c.name for c in wa.chats(limit=40) if not c.name.startswith("+")]
        except Exception:
            pass
        seen, out = set(), []
        for n in names:
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        self.hearing.vocabulary = ", ".join(out[:70])

    def _listen_from(self, frames) -> str:
        source = frames

        def frames():
            if self._fn_pressed():
                raise DictationRequested()
            return source()
        until = time.monotonic() + max(self.cfg.listen.start_timeout_s, 1.0)
        old = self.cfg.listen.start_timeout_s
        try:
            while True:
                audio = record_command(frames, self.cfg, self.detector, on_level=self.hud.level,
                                       still_talking=self._still_talking if self.cfg.listen.smart_end else None)
                if audio is None:
                    return ""
                audio = self._only_you(audio)
                if audio is not None:
                    break
                # Someone else talking: keep listening for you for whatever time is left.
                left = until - time.monotonic()
                if left <= 0.3:
                    return ""
                self.cfg.listen.start_timeout_s = left
        finally:
            self.cfg.listen.start_timeout_s = old
        self._refresh_vocabulary()
        self.hud.show("thinking", subtext="Processing")
        t = time.monotonic()
        text = self.hearing.transcribe(audio)
        # When you say it in one breath, the end of "Hey Jarvis" lands in the command.
        text = re.sub(r"^\W*(hey\W+)?(jarvis|javis|jervis|jarvas)\b\W*", "", text, flags=re.I).strip()
        self.log(f"  (heard in {time.monotonic() - t:.2f}s, {len(audio) / 16000:.1f}s of speech)")
        self.transcripts.append(text)
        return text

    def ask(self, question: str, state: str = "confirm", subtext: str = "", hud_text: str = "", hud_sub: str = "") -> str:
        """Ask something and return your answer. Answering while he's still asking counts too."""
        try:
            self.say(question, state=state, subtext=subtext)
        except Interrupted as e:
            return e.text
        self.hud.show(state, text=hud_text or question, subtext=hud_sub or "Say yes or no")
        return self.listen(timeout=self.cfg.safety.confirm_timeout_s)

    def confirm(self, question: str, detail: str = "") -> bool:
        """Ask before acting. Only a clear spoken yes returns True.

        Every skill marked "confirm" passes through here; the AI model cannot skip it.
        """
        answer = self.ask(question, state="confirm", subtext=detail or "Awaiting your word",
                          hud_text=detail or question, hud_sub="Say yes or no")
        ok = safety.is_yes(answer, self.cfg.safety.yes_words, self.cfg.safety.no_words)
        safety.log("confirmation", question=question, heard=answer, approved=ok)
        if not ok:
            self.say(persona.pick(persona.NO_ANSWER if not answer.strip() else persona.CANCELLED))
        return ok

    # ---------- doing ----------

    def run_skill(self, name: str, args: dict | None = None) -> Result | None:
        """Run one skill, asking first if it needs a yes. None means you said no."""
        sk = skills.REGISTRY.get(name)
        if not sk:
            safety.log("unknown_skill", name=name)
            return Result("I'm afraid I can't do that yet, Sir.", ok=False)
        args = skills.clean_args(sk, args or {})
        if any(r not in args for r in sk.required):
            return Result("I didn't quite get the details, Sir.", ok=False)
        if sk.tier == "message":
            return self.send_message_flow(**args)
        if sk.tier == "confirm":
            question, detail = sk.confirm(**args)
            if not self.confirm(question, detail):
                return None
        self._remember_undo(name, args)
        safety.log("action", name=name, args=args)
        self.actions.append((name, args))
        self._recent_skills = (self._recent_skills + [name])[-8:]
        try:
            result = sk.fn(**args)
        except Exception as e:
            safety.log("action_failed", name=name, error=type(e).__name__)
            result = Result("That didn't work, I'm afraid, Sir.", ok=False)
        if name == "go_offline":
            self.running = False
        return result

    # ---------- messages ----------

    def send_message_flow(self, to: str, message: str = "", app: str = "") -> Result | None:
        """Find the person, read the message back, allow changes, and send only on a clear yes.

        Returns None when nothing was sent because you said no (Jarvis has already said so).
        """
        mc = self.cfg.messaging
        app = app if app in ("whatsapp", "imessage") else mc.default_app
        app_name = "WhatsApp" if app == "whatsapp" else "iMessage"
        msg = tidy_message(message)
        if msg and not words_from(msg, getattr(self, "_request", msg), to):
            # None of the message's words came from you: the AI made it up. Ask instead.
            safety.log("invented_message_dropped")
            msg = ""
        if not msg:
            msg = tidy_message(self.ask("What should the message say, Sir?", state="speaking", hud_sub="Listening"))
            if not msg:
                return Result("Never mind, Sir. Nothing was sent.", ok=False)
        if not self.rate.allowed():
            safety.log("message_rate_limited")
            return Result("I've sent several messages very recently, Sir. Let's give it a minute.", ok=False)

        try:
            wa = None if getattr(self, "_no_whatsapp", False) else skills.whatsapp_or_none()
            people = messaging.resolve(to, whatsapp=wa, lookup=getattr(self, "_contacts_lookup", None))
        except messaging.MessagingError as e:
            return Result(e.spoken, ok=False)
        if not people:
            return Result(f"I can't find {to} in your WhatsApp chats, contacts or the numbers I remember, Sir. "
                          f"You can say the number instead.", ok=False)
        same = [p for p in people if p.name.lower() == messaging._clean_name(to)]
        distinct = {re.sub(r"\W+", " ", p.name.lower()).strip() + p.kind for p in people[:3]}
        # Ask only when there's a real choice between different names; never between identical ones.
        person = people[0] if len(people) == 1 or len(same) == 1 or len(distinct) == 1 \
            else self._choose_contact(to, people)
        if person is None:
            return None
        if person.source == "spoken":
            problem = messaging.number_problem(person.name, mc.country_code or None)   # the digits as you said them
            if problem:
                return Result(problem, ok=False)
        if app == "imessage" and not person.number:
            return Result(f"I can only send iMessages to a phone number, Sir, and {person.name} has none here.", ok=False)
        number = person.number
        if person.source == "spoken" and number:
            # A number you said that belongs to someone known: use their name, not the digits.
            digits = re.sub(r"\D", "", number)
            known = next((p["name"] for p in messaging.load_people().values()
                          if re.sub(r"\D", "", p["number"]) == digits), None) or \
                next((c.name for c in (wa.chats() if wa else []) if c.phone == digits and not c.name.startswith("+")), None)
            if known:
                person = messaging.Target(known, number=number, source="book")
        if person.kind == "group":
            who = f"the {person.name} group"
        elif person.source == "spoken":
            known = next((c.name for c in (wa.chats() if wa else []) if c.phone and number and
                          c.phone == re.sub(r"\D", "", number) and not c.name.startswith("+")), None)
            who = f"{messaging.say_digits(number)}" + (f", that's {known} on WhatsApp" if known else "")
        else:
            who = person.name

        # Read it back. "Change it to …" rewrites it; only a clear yes sends.
        for _ in range(mc.max_edits + 1):
            question = f"Message to {who} on {app_name}: {msg} Do I send it, Sir?" if msg[-1] in ".!?" else \
                f"Message to {who} on {app_name}: {msg}. Do I send it, Sir?"
            answer = self.ask(question, subtext=f"To {person.name} · {app_name}", hud_text=f"“{msg}”",
                              hud_sub=f"To {person.name} · {app_name} · say yes or no")
            changed = edited_message(answer)
            if changed:
                msg = tidy_message(changed)
                safety.log("message_changed", to=person.name)
                continue
            ok = safety.is_yes(answer, self.cfg.safety.yes_words, self.cfg.safety.no_words)
            safety.log("confirmation", question=f"message to {person.name} ({app_name})",
                       heard=answer if len(answer.split()) <= 4 else "(a longer answer)", approved=ok)
            if not ok:
                self.say(persona.pick(persona.NO_ANSWER if not answer.strip() else persona.CANCELLED))
                return None
            break
        else:
            return Result("Let's start that one again another time, Sir. Nothing was sent.", ok=False)

        self.hud.show("thinking", text=f"“{msg}”", subtext=f"Sending to {person.name}")
        try:
            if getattr(self, "_screen_locked", messaging.screen_locked)():
                raise messaging.MessagingError("locked", messaging._WA_ERRORS["locked"])
            sender = getattr(self, "_sender", None)
            if sender:
                sender(app, person, msg)
            elif app == "imessage":
                messaging.send_imessage(number, msg, mc.country_code or None)
            elif number:
                messaging.send_whatsapp(number, msg, mc.country_code or None)     # WhatsApp's own link, by number
            else:
                messaging.send_whatsapp_chat(person.chat or person.name, msg)   # groups: through WhatsApp's search
        except messaging.MessagingError as e:
            safety.log("message_not_sent", to=person.name, app=app, reason=e.code)
            return Result(e.spoken, ok=False)
        self.rate.record()
        self.undo_stack = (self.undo_stack + [("A sent message can't be unsent by me, Sir. You can delete it in "
                                               f"{app_name} yourself.", None)])[-10:]
        self.sent_messages.append({"to": person.name, "app": app, "text": msg, "kind": person.kind})
        details = {"to": person.name, "app": app, "kind": person.kind}
        if mc.log_text:
            details["text"] = msg
        safety.log("message_sent", **details)
        return Result("Sent to that number, Sir." if person.source == "spoken" else
                      f"Sent to {person.name}{' group' if person.kind == 'group' and 'group' not in person.name.lower() else ''}, Sir.")

    def _choose_contact(self, said: str, people: list) -> object | None:
        """Several contacts match: ask which one."""
        options = people[:3]
        names = [p.name + (" (the group)" if getattr(p, "kind", "") == "group" else "") for p in options]
        listing = ", ".join(names[:-1]) + f", or {names[-1]}" if len(names) > 2 else " or ".join(names)
        answer = self.ask(f"There's more than one {said}, Sir: {listing}. Which one?",
                          subtext="Which contact?", hud_sub="Which one?").lower()
        if not answer.strip() or safety.is_stop(answer) or re.search(r"\b(none|neither|cancel|never mind)\b", answer):
            self.say("Very well, Sir. Nothing was sent.")
            return None
        ordinals = {"first": 0, "one": 0, "second": 1, "two": 1, "third": 2, "three": 2, "last": len(options) - 1}
        for word, i in ordinals.items():
            if re.search(rf"\b(the )?{word}\b", answer) and i < len(options):
                return options[i]
        words = set(re.findall(r"[a-z]+", answer))
        scored = sorted(options, key=lambda p: -len(words & set(re.findall(r"[a-z]+", p.name.lower()))))
        best = scored[0]
        if words & set(re.findall(r"[a-z]+", best.name.lower())) - set(re.findall(r"[a-z]+", said.lower())):
            return best
        self.say("I didn't catch which one, Sir. Nothing was sent.")
        return None

    # ---------- understanding ----------

    def handle(self, text: str):
        self._request = text
        safety.log("command", heard=redact_command(text))
        if not re.sub(r"[^a-z]", "", text.lower()):
            self.say(persona.pick(persona.DIDNT_CATCH))
            return
        if self.fast_path(text):
            return
        self.ask_brain(text)

    def fast_path(self, text: str) -> bool:
        """Instant answers for everyday commands, without waiting for the AI model."""
        t = text.lower().strip(" .!?")
        # "Can you…", "please…", "…for me": the request is the same without them.
        for _ in range(2):
            t = re.sub(r"^(?:can you|could you|would you|will you|please|jarvis|hey jarvis|kindly|just)[, ]+", "", t)
            t = re.sub(r"[, ]+(?:for me|please|now|right now|jarvis|sir)$", "", t).strip(" .!?,")
        words = t.split()

        def do(skill_name, **args):
            r = self.run_skill(skill_name, args)
            if r is not None:
                self.say(r.say)
            return True

        if safety.is_stop(text) and t in ("stop", "stop it", "jarvis stop") and music_is_playing():
            return do("media", action="pause")      # "stop" while music plays means the music
        if safety.is_stop(text):
            self.say(persona.pick(persona.STAND_BY))
            self.end_conversation = True      # "stop", "never mind", "that's all": he stops listening too
            return True
        # Two requests in one ("open Spotify and turn it up") go to the brain, which can do both.
        content_first = re.match(r"^(please )?((take|make|write)( me)? (a )?note|note down|jot down|note that|remember|"
                                 r"keep in mind|remind me|forget)\b", t)
        if (len(words) > 3 and re.search(r"\b(and|then|also|after that)\b", t) and not content_first
                and not re.search(r"\btimer\b", t)):
            return False
        if re.search(r"\b(shut ?down|restart|reboot|sleep)\b.*\b(mac|computer|laptop|system|macbook)\b", t):
            action = "restart" if re.search(r"restart|reboot", t) else "sleep" if "sleep" in t else "shut_down"
            return do("power", action=action)
        if re.search(r"^(please )?(go offline|shut ?down|power down|turn (yourself )?off|goodbye|good night|exit|quit)( jarvis)?$", t):
            r = self.run_skill("go_offline")
            if r is not None:
                self.say(persona.pick(persona.GOODBYE), interruptible=False)
            return True
        if re.search(r"^(what('s| is) the time|what time is it( now)?|what's the time now|tell me the time|time please|"
                     r"current time|the time)( sir| jarvis)?$", t):
            self.say(persona.time_now())
            return True
        if re.search(r"^(what('s| is) (the |today'?s )?date( today)?|what day is (it|today)|today'?s date)( sir| jarvis)?$", t):
            self.say(persona.date_today())
            return True
        if re.search(r"^(who are you|what are you|introduce yourself)$", t):
            self.say("I'm Jarvis, Sir. Your assistant. I run entirely on this Mac, and I ask before I act.")
            return True
        if re.search(r"^(what can you do|what are your (skills|abilities)|help|what do you do)$", t):
            self.say(CAPABILITIES)
            return True
        if re.search(r"^(good morning|morning|brief me|daily briefing|what'?s my day( like)?|give me (a|my) briefing)( jarvis| sir)?$", t):
            r = self.run_skill("briefing")
            if r is not None:
                try:
                    self.last_answer = self.say_stream(sentences(self.brain.report_for(text, "briefing", r.data or r.say)))
                except BrainUnavailable:
                    self.say(r.say)
                self.follow_up_ok = True
            return True
        if re.search(r"^copy (that|it|this)( to (the |my )?clipboard)?$", t):
            if not self.last_answer:
                self.say("There's nothing to copy yet, Sir.")
                return True
            return do("copy_to_clipboard", text=self.last_answer)
        if _UNDO.match(t) and t not in ("cancel", "no no"):
            self.say(self.undo_last())
            return True
        if _END.match(t):
            self.say("Very good, Sir.")
            self.end_conversation = True
            return True
        if re.search(r"^(thank you|thanks)( (so much|sir))?$", t):
            self.say("Always a pleasure, Sir.")
            self.end_conversation = True
            return True
        if re.search(r"^(repeat that|say that again|repeat|pardon|come again|what did you say|sorry what)$", t):
            self.say(self.last_answer or "I haven't said anything worth repeating yet, Sir.")
            return True
        m = re.search(r"^(?:please )?(?:speak|talk)(?: a (?:bit|little))? (slower|more slowly|faster|quicker|more quickly)$", t)
        if m:
            step = -0.08 if m.group(1).startswith(("slower", "more slowly")) else 0.08
            self.voice.cfg.speed = round(max(0.8, min(1.3, self.voice.cfg.speed + step)), 2)
            settings.save(voice_speed=self.voice.cfg.speed)
            self.say("Is this better, Sir?")
            return True

        # WhatsApp: reading new messages aloud, on or off
        if re.search(r"\b(don'?t|do not|stop|never|no more)\b.*\b(read|announc\w*|reading)\b.*\bmessages?\b|"
                     r"\bmessages?\b.*\b(silently|quietly)\b", t):
            return do("message_alerts", state="off")
        m = re.search(r"\b(read|announce|tell me)\b.*\bmessages?\b(?: from (?P<chat>.+?))?\s*(?:aloud |out loud )?"
                      r"\b(when|as|whenever)\b.*\b(arrive|arrives|come|comes|come in|comes in|get|receive)\b", t)
        if m:
            return do("message_alerts", state="on", chat=(m.group("chat") or "").strip())
        if re.search(r"^(any|do i have any|check( my)?|what are my|read( me)?( my)?)( new| unread)?( whatsapp)? messages\??$|"
                     r"^what did i miss( on whatsapp)?$|^anything new on whatsapp$", t):
            r = self.run_skill("whatsapp_unread")
            if r is not None and r.ok and r.data and "No unread" not in r.data:
                try:
                    self.say_stream(sentences(self.brain.report_for(text, "whatsapp_unread", r.data)))
                except BrainUnavailable:
                    self.say(r.say)
            elif r is not None:
                self.say(r.say)
            return True

        # People and nicknames: "remember Rohan's number is 98200 12345", "remember Maple Tuition as my tuition group"
        m = re.match(r"^(?:please )?(?:remember|save|note)(?: that)? (.+?)(?:'s| s)? (?:phone )?number (?:is|as) (.+)$", t)
        if m and messaging.spoken_number(m.group(2)):
            return do("remember_person", name=_original(text, m.group(1)), number=m.group(2))
        m = re.match(r"^(?:please )?(?:remember|keep in mind)(?: that)? (.+?) (?:as|is) my (.+)$", t)
        if m and len(m.group(2).split()) <= 4:
            r = self.run_skill("nickname", {"said": m.group(2), "target": _original(text, m.group(1))})
            if r is not None and r.ok:
                self.say(r.say)
                return True
            # Not a chat or contact: keep it as a fact instead.

        # How he addresses you, and your country code for phone numbers.
        m = re.search(r"^(?:my country code is|set my country code to|country code) \+?\s*(\d{1,4})$", t)
        if m:
            settings.save(country_code=m.group(1))
            self.cfg.messaging.country_code = m.group(1)
            self.say(f"Noted, Sir. Numbers without a country code will get plus {' '.join(m.group(1))}.")
            return True
        said = re.sub(r"^(?:hey jarvis|jarvis|please)[, ]+", "", text.lower().strip(" .!?"))   # "sir" kept here
        m = re.search(r"^(?:please )?(?:call me|address me as|you can call me) (sir|ma'?am|madam|boss|[a-z][a-z' -]{1,24})$", said)
        if m and not re.search(r"\b(back|later|when|if|at|tomorrow|tonight)\b", m.group(1)):
            who = {"sir": "Sir", "maam": "Ma'am", "ma'am": "Ma'am", "madam": "Madam"}.get(m.group(1), m.group(1).title())
            settings.save(address=who)
            self.cfg.name_for_user = who
            self.brain.user = who
            self.say(f"Very well, {who}.")
            return True

        # fn dictation: on/off, how much polishing, paste the last one again, and how to set it up.
        if re.search(r"^(turn |switch )?(off|stop|disable) (the )?(fn |function key )?(dictation|voice typing)( off)?$|"
                     r"^(turn|switch) (the )?(fn |function key )?(dictation|voice typing) off$", t):
            settings.save(dictation=False)
            self.stop_dictation()
            self.say("Done, Sir. The fn key is back to normal; no more dictation until you ask.")
            return True
        if re.search(r"^(turn |switch )?(on|start|enable) (the )?(fn |function key )?(dictation|voice typing)( on)?$|"
                     r"^(turn|switch) (the )?(fn |function key )?(dictation|voice typing) on$", t):
            settings.save(dictation=True)
            ok = self.start_dictation()
            note = self.dictation_setup_note() if ok else ""
            self.say("Dictation is on, Sir. Hold fn and speak; let go and I'll type it. " + note if ok else
                     "I couldn't start dictation, Sir.")
            return True
        m = re.search(r"^(?:polish|clean up|correct|edit) (?:my )?(?:the )?dictation (fully|properly|lightly|a little|"
                      r"not at all|less|more)$|^(?:dictation|voice typing) (?:polish(?:ing)?|clean ?up) (full|light|off|on)$|"
                      r"^(?:don'?t|do not) (?:polish|clean up|correct|change) (?:my )?(?:dictation|what i dictate)$|"
                      r"^type (?:exactly )?what i say(?: exactly)?$", t)
        if m:
            how = (m.group(1) or m.group(2) or "").lower()
            level = ("light" if how in ("lightly", "a little", "less", "light") else
                     "full" if how in ("fully", "properly", "more", "full", "on") else "off")
            settings.save(dictation_polish=level)
            self.say({"full": "Understood, Sir. I'll fix grammar, mistakes and lists as I type.",
                      "light": "Understood, Sir. Only the ums, stutters and punctuation from now on.",
                      "off": "Understood, Sir. I'll type exactly what I hear."}[level])
            return True
        if re.search(r"^(paste|type|put) (that|it|the last (one|dictation)) (again|in again|back)$|^paste again$", t):
            if self.dictation is not None and self.dictation.paste_again():
                self.hud.hide()
                self.end_conversation = True
                safety.log("dictation", outcome="pasted again")
            else:
                self.say("There's nothing recent to paste, Sir. I only keep the last dictation for five minutes.")
            return True
        if re.search(r"^(how (do i|to) (use|set up|setup) (fn |the fn key |voice )?(dictation|typing)|"
                     r"(set ?up|check) (fn |voice )?dictation|is dictation (working|set up|ready))$", t):
            if self.dictation is None:
                self.say("Dictation is off, Sir. Say 'turn on dictation' and I'll start it.")
                return True
            note = self.dictation_setup_note()
            self.say("Hold the fn key and speak, Sir; let go and I'll type it, cleaned up, wherever your cursor is. "
                     "Tap fn twice to talk hands-free, and tap it once more to finish. "
                     + (note or "Everything is set up."))
            return True

        # Your voice: "learn my voice", "forget my voice", "listen to everyone", "only listen to me"
        if re.search(r"^(learn|train|remember|recogni[sz]e|record|memori[sz]e|save)( my| to my)? voice( again)?$|"
                     r"^(set up|start) (voice training|voice recognition)$|^voice training$", t):
            self.train_voice()
            return True
        if re.search(r"^(forget|delete|erase) my voice( print)?$", t):
            if self.voiceprint:
                self.voiceprint.forget()
            self.say("Done, Sir. I've forgotten your voice, so I'll listen to anyone again.")
            return True
        if re.search(r"^(listen to everyone|listen to anybody|anyone can talk to you|stop recogni[sz]ing my voice)$", t):
            settings.save(only_my_voice=False)
            self.say("Very well, Sir. I'll listen to anyone.")
            return True
        if re.search(r"^(only listen to me|listen only to me|just listen to me|only my voice)$", t):
            settings.save(only_my_voice=True)
            if self.voiceprint and self.voiceprint.trained:
                self.say("Understood, Sir. Only you from now on.")
            else:
                self.say("I'll need to learn your voice first, Sir.")
                self.train_voice()
            return True

        # Which music app you use: "I use Apple Music", "use Spotify"
        m = re.match(r"^(?:i use |use |with |i listen on |i listen to music on |play it on )?(apple music|the music app|music app|spotify)$", t)
        if m and (self._recent_skills[-1:] in (["play_music"], ["media"]) or t.startswith(("i use", "use"))):
            return do("set_music_app", app="Spotify" if "spotify" in m.group(1) else "Music")

        # Volume
        m = re.search(r"\b(?:volume|sound)\b.*?\b(\d{1,3})\b|\bset (?:the )?volume to (\d{1,3})", t)
        if m and len(words) <= 8:
            return do("volume", action="set", level=int(m.group(1) or m.group(2)))
        if re.search(r"^(unmute|sound on)\b", t):
            return do("volume", action="unmute")
        if re.search(r"^(mute|silence)\b|\bmute (the )?(sound|volume|mac)\b", t):
            return do("volume", action="mute")
        if len(words) <= 6 and re.search(r"\b(louder|volume up|turn (it|the volume|the sound) up|increase (the )?volume)\b", t):
            return do("volume", action="up")
        if len(words) <= 6 and re.search(r"\b(quieter|softer|volume down|turn (it|the volume|the sound) down|lower (the )?volume|decrease (the )?volume)\b", t):
            return do("volume", action="down")

        # Music: every everyday music command, instantly
        hit = music_command(t)
        if hit:
            return do(hit[0], **hit[1])

        # The screen, only when you ask: "read my screen", "what's on my screen?"
        m = re.match(r"^(?:please |can you |could you )?(?:read (?:out )?(?:me )?(?:the )?(?:text on )?(?:my |the )?"
                     r"(screen|window|page)|read (?:this|that)(?: out)?(?: for me)?|read out (?:what'?s on )?(?:my |the )?(screen|window|page))"
                     r"(?: aloud| out loud| to me)?$", t)
        if m:
            area = "screen" if re.search(r"\b(whole|entire|full|all of)\b", t) else "window"
            self.hud.show("thinking", text="", subtext="Reading your screen")
            return do("read_screen_text", area=area)
        if re.search(r"^(?:what'?s|what is) on (?:my |the )?(?:whole |entire )?(screen|window)$|^what am i looking at$|"
                     r"^look at (?:my |the )?(?:whole |entire )?screen$", t):
            self.hud.show("thinking", text="", subtext="Looking at your screen")
            return do("look_at_screen", question="Describe what is on the screen.",
                      area="screen" if re.search(r"\b(whole|entire|full|all of)\b", t) else "window")

        # "Open it" / "open the second one", right after a file search
        m = re.match(r"^(?:please )?open (?:it|that|that one|this|the (first|second|third|fourth|fifth|last|newest) one|"
                     r"number (\d))$", t)
        if m and "find_files" in self._recent_skills[-4:]:
            pick = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "newest": 1, "last": 5}
            n = int(m.group(2)) if m.group(2) else pick.get(m.group(1) or "first", 1)
            return do("open_found_file", number=n)

        # Open an app or a well-known site
        m = re.match(r"^(?:please )?(?:open|launch|start|switch to|bring up)\s+(?:the |my )?(.+?)(?: app)?$", t)
        if m and len(m.group(1).split()) <= 3:
            target = m.group(1)
            if skills.find_app(target) and difflib_close(target, skills.find_app(target)[0]):
                return do("open_app", name=target)
            if target.replace(".com", "").strip() in skills.KNOWN_SITES:
                return do("open_website", site=target)

        # Memory: "remember that my sister's birthday is March 3rd" / "forget my sister's birthday"
        m = re.match(r"^(?:please )?(?:remember|keep in mind|note that)(?: that)? (.{4,})$", t)
        if m and not re.match(r"^(to |when |what |where |who |how )", m.group(1)):
            return do("remember", fact=about_sir(text[text.lower().index(m.group(1)):]))
        m = re.match(r"^(?:please )?forget (?:that |about |what i said about )?(.{3,})$", t)
        if m:
            return do("forget", about=about_sir(m.group(1)))

        # Messages, when the words are unmistakable:
        #   "send a WhatsApp to Rahul saying I'm on my way", "message Mum that I'll be late",
        #   "iMessage Priya saying see you soon". Anything else ("tell Mum…") goes to the brain.
        m = _MESSAGE.match(t)
        if m:
            app = "imessage" if re.search(r"imessage|i message", t) else ("whatsapp" if "whats" in t else "")
            to = m.group("to").strip()
            at = text.lower().rfind(m.group("body"))
            body = text[at:].strip() if at >= 0 else m.group("body")   # keep your capitals and punctuation
            r = self.run_skill("send_message", {"to": to, "message": body, "app": app})
            if r is not None:
                self.say(r.say)
            return True

        # Notes: "take a note: buy milk", "note down the wifi password is on the router"
        m = re.match(r"^(?:please )?(?:(?:take|make|write)(?: me)? (?:a )?note|note down|jot down)[:,]?\s*(?:that |saying )?(.{3,})$", t)
        if m:
            return do("create_note", text=text[text.lower().index(m.group(1)):].strip())

        # Appearance
        if re.search(r"^(switch to |turn on |enable |go )?dark mode( on| please)?$", t):
            return do("dark_mode", mode="on")
        if re.search(r"^(switch to |turn on |enable |go )?light mode( on| please)?$|^(turn off|disable) dark mode$|^dark mode off$", t):
            return do("dark_mode", mode="off")

        # Reminders: "remind me to call mum at 6", "remind me in 20 minutes to check the oven"
        m = re.match(r"^(?:please )?remind me (?:to |about )?(.+)$", t)
        if m:
            rest = m.group(1)
            tm = _TIME_PHRASE.search(rest)
            if tm:
                when = rest[tm.start():].strip()
                what = (rest[:tm.start()] + " " + rest[tm.end():]).strip() if tm.start() == 0 else rest[:tm.start()].strip()
                if tm.start() == 0:   # "remind me in 20 minutes to check the oven"
                    when_m = re.match(r"^(.+?)\s+(?:to|about)\s+(.+)$", rest)
                    if when_m:
                        when, what = when_m.group(1), when_m.group(2)
                what = re.sub(r"^(to|about)\s+", "", what).strip()
                if what and parse_when(when):
                    return do("add_reminder", text=what, when=when)

        # Timers
        if re.search(r"\btimer\b", t) and re.search(r"\b(set|start|put|timer for)\b", t):
            secs = duration_seconds(t)
            if secs:
                label = ""
                lm = re.search(r"\bfor (?:the |my )?([a-z][a-z ]{1,30})$", t)
                if lm and not duration_seconds(lm.group(1)):
                    label = lm.group(1).strip()
                return do("set_timer", duration=t, label=label)
        if re.search(r"^(cancel|stop|delete) (all )?(my |the )?timers?$", t):
            return do("timers", action="cancel_all")
        return False

    def ask_brain(self, text: str):
        """Let the AI model answer or choose skills."""
        start = len(self.spoken)
        try:
            self._ask_brain(text)
        finally:
            # "Copy that" copies what Jarvis said in answer to this request, and nothing older.
            said = [s for s in self.spoken[start:] if not s.startswith("Shall I")]
            if said:
                self.last_answer = " ".join(said)

    def _ask_brain(self, text: str):
        self.hud.show("thinking", text=f"“{text}”", subtext="Thinking")
        try:
            already_said = self.say_stream(sentences(self.brain.chat(text)))
        except BrainUnavailable:
            safety.log("brain_unavailable")
            self.say("My reasoning module isn't responding, Sir. I can still do the basics: time, volume, "
                     "music, apps and timers.", state="error", subtext="Brain offline")
            return
        calls = self.brain.tool_calls[: self.cfg.brain.max_tool_calls]
        if not calls:
            # A tool call written out as text instead of made: run it properly.
            calls = pseudo_tool_calls(getattr(self.brain, "_last_text", ""))[: self.cfg.brain.max_tool_calls]
        if calls:
            # The calculator for a question with no numbers in it ("when was he born?"), or day
            # counting for a question that isn't about counting days, is a slip.
            kept = [c for c in calls if not (c["name"] == "calculate" and not _MATHS.search(text))
                    and not (c["name"] == "date_math" and not _DAYCOUNT.search(text))]
            if not kept and not already_said:
                self.say_stream(sentences(self.brain.chat("(No calculation was needed. Answer my last question "
                                                          "from your own knowledge.)")))
                return
            calls = kept
        if not calls and _PROMISE.search(already_said):
            # He said he'd do something but didn't call the skill. Ask him once to actually do it.
            list(self.brain.chat("(You said you would do that but called no tool. Call the right tool now. "
                                 "If no tool can do it, reply exactly: NO TOOL.)"))
            calls = self.brain.tool_calls[: self.cfg.brain.max_tool_calls]
            already_said = ""
        if not calls:
            return
        untrusted_seen = False
        for step in range(self.cfg.brain.max_steps):
            done_lines, info, results = [], [], []
            for c in calls[: self.cfg.brain.max_tool_calls]:
                sk = skills.REGISTRY.get(c["name"])
                # Something read from messages, the clipboard or the web can't make him act on its own:
                # a send or a risky step after reading such things needs your own words to ask for it.
                if c["name"] in SCREEN_SKILLS and not _SCREEN_WORDS.search(text):
                    safety.log("screen_look_refused")
                    results.append((c["name"], "Not done: Sir didn't ask about his screen."))
                    continue
                if untrusted_seen and sk and sk.tier in ("message", "confirm") and not _ASKED_TO_ACT.search(text):
                    safety.log("suggested_action_dropped", name=c["name"])
                    results.append((c["name"], "Not done: Sir didn't ask for that."))
                    continue
                r = self.run_skill(c["name"], c["arguments"])
                if r is None:                     # you said no; Jarvis has already said so
                    results.append((c["name"], "Sir declined, so it was not done."))
                    continue
                if c["name"] in UNTRUSTED:
                    untrusted_seen = True
                if c["name"] in ("look_up", "news", "weather") and not r.ok:
                    # No web just now: answer from what he knows, saying so, rather than giving up.
                    r = Result(r.say, data="The web couldn't be reached. Answer Sir's question from your own knowledge "
                                           "if you can, and mention it might be out of date.")
                data = r.data or r.say
                if c["name"] == "look_up" and r.ok:
                    data += ("\n(If these results don't answer Sir's question, answer it from your own knowledge "
                             "instead of saying you couldn't find it.)")
                results.append((c["name"], data))
                if sk and sk.kind == "info" and r.ok and r.data:
                    info.append(r)
                elif not already_said or not r.ok:
                    done_lines.append(r.say)
            if done_lines:
                self.say(" ".join(done_lines))
            if not info:
                self.brain.note_tool_results(results)
                return
            # He has information to work with: he answers from it, and may take a further step
            # ("…and remind me an hour before").
            try:
                self.say_stream(sentences(self.brain.report(results, allow_tools=step + 1 < self.cfg.brain.max_steps)))
            except BrainUnavailable:
                self.say(" ".join(r.say for r in info))
                return
            calls = self.brain.tool_calls
            spoken_now = getattr(self.brain, "_last_text", "")
            if not calls and step + 1 < self.cfg.brain.max_steps and _PROMISE.search(spoken_now):
                # He said he'd do the next part ("I'll remind you") but didn't: ask him once to actually do it.
                list(self.brain.chat("(You said you would do that but called no tool. Call the right tool now. "
                                     "If no tool can do it, reply exactly: NO TOOL.)"))
                calls = self.brain.tool_calls
            already_said = spoken_now
            if not calls:
                return

    # ---------- the loop ----------

    def on_wake(self, score: float, recent: list[np.ndarray]):
        """Called when the wake-word model fires. `recent` is the last two seconds of audio."""
        self.hud.show("waking", text="", subtext="")
        # Second check: did you really say "Jarvis"? (Not "Travis", not the TV.)
        heard_ok = self.hearing.heard_name(np.concatenate(recent).astype(np.float32) / 32768)
        word = getattr(self.hearing, "last_check_word", "")
        if not heard_ok and score >= self.cfg.wake.trust_score and word not in _NOT_JARVIS:
            heard_ok = True        # very sure it was "Hey Jarvis"; the second check just misheard it
        if not heard_ok:
            # Only the one word closest to "Jarvis" is logged, so the check can be tuned to your voice.
            safety.log("wake_ignored", score=round(score, 2), heard_as=getattr(self.hearing, "last_check_word", ""))
            self.log(f"  (wake word fired at {score:.2f}, but it wasn't “Jarvis”; ignored)")
            self.hud.hide()
            self.wake.reset()
            return
        if not self._is_you(np.concatenate(recent[-20:]).astype(np.float32) / 32768, "wake word"):
            self.hud.hide()          # "Hey Jarvis" in someone else's voice
            self.wake.reset()
            return
        safety.log("wake", score=round(score, 2))
        self.log(f"* Hey Jarvis heard (score {score:.2f})")
        self.brain.warm()   # start loading the model while you talk

        # Did you carry straight on ("Hey Jarvis, what time is it")? Look at the
        # audio that arrived since the wake word, waiting up to ~0.45 s.
        early: list[np.ndarray] = []
        frame_s = self.cfg.frame_samples / self.cfg.sample_rate
        self.detector.reset()
        speech_frames = 0
        while len(early) * frame_s < 0.45:
            f = self.mic.read(timeout=0.3)
            if f is None:
                break
            early.append(f)
            if len(early) > 2 and self.detector.prob(f) >= self.cfg.listen.speech_threshold:
                speech_frames += 1
        carried_on = speech_frames >= 2

        if carried_on:
            self.hud.show("listening", text="", subtext="Listening")
            queued = list(early)

            def frames():
                return queued.pop(0) if queued else self.mic.read(timeout=1.0)
            text = self._listen_from(frames)
        else:
            line = persona.pick(persona.WAKE)
            self.hud.show("listening", text=line, subtext="Listening")
            self.chime("wake")
            if self.speak_enabled:
                self.voice.play(self.voice.render(line))
            self.spoken.append(line)
            self._after_speaking()
            self.hud.show("listening", text=line, subtext="Listening")
            text = self.listen()

        self.converse(text)

    def converse(self, text: str, opening: bool = False):
        """Your request, then more without "Hey Jarvis": after each reply he listens a few seconds.

        `opening`: called right after his greeting, so he simply listens first.
        """
        self.end_conversation = False
        if opening:
            self.hud.show("listening", text="", subtext="Listening, Sir")
            text = self.listen(timeout=self.cfg.follow_up_s)
        for turn in range(1 + self.cfg.brain_follow_ups):
            if not text:
                if turn == 0 and not opening:
                    self.log("  (nothing said)")
                break
            self.log(f"You: {text}")
            self.hud.show("thinking", text=f"“{text}”", subtext="Working on it")
            try:
                self.handle(text)
            except Interrupted as e:
                nxt = self.on_interruption(e.text)
                if nxt:
                    text = nxt
                    continue
            except Exception as e:
                # One request going wrong must never stop Jarvis.
                safety.log_error("handling a request", e)
                try:
                    self.say("Sorry, Sir, something went wrong with that. I'm still here.", state="error",
                             subtext="Something went wrong")
                except Exception:
                    pass
            if not self.running or self.end_conversation:
                break
            self.hud.show("listening", text="", subtext="Anything else, Sir?")
            text = self.listen(timeout=self.cfg.follow_up_s)
        self.hud.hide()
        self._after_speaking()

    # ---------- voice training ----------

    TRAINING = ["Hey Jarvis.",
                "Hey Jarvis, what's the weather like today?",
                "Hey Jarvis, send a message to Mummy saying I'll be home soon.",
                "Hey Jarvis, play some music by Arijit Singh.",
                "Hey Jarvis, set a timer for ten minutes.",
                "Hey Jarvis, check for updates in my tuition group.",
                "Hey Jarvis, remind me to call the bank tomorrow at nine.",
                "Hey Jarvis, what time is it?"]

    def train_voice(self):
        """Like Siri's setup: you say eight sentences; Jarvis learns your voice (and which speech model
        hears you best). The recordings are thrown away; only the voiceprint numbers are kept."""
        if self.voiceprint is None:
            self.say("Voice recognition isn't available, Sir. Its model is missing.")
            return
        settings.save(voice_training_offered=True)
        self.say("Let's learn your voice, Sir. I'll show a sentence on screen and say it; then you say it, "
                 "in your normal voice, from where you usually sit. Eight sentences in all.", interruptible=False)
        clips, texts = [], []
        old = (self.cfg.listen.start_timeout_s, self.cfg.listen.max_command_s, self.cfg.listen.smart_end)
        self.cfg.listen.start_timeout_s, self.cfg.listen.max_command_s, self.cfg.listen.smart_end = 8.0, 9.0, False
        try:
            for i, sentence in enumerate(self.TRAINING, 1):
                for attempt in range(2):
                    self.hud.show("confirm", text=f"“{sentence}”", subtext=f"Say this · {i} of {len(self.TRAINING)}")
                    self.say(("Please say: " if attempt == 0 else "Once more, please: ") + sentence,
                             state="confirm", subtext=f"{i} of {len(self.TRAINING)}", interruptible=False)
                    self.hud.show("listening", text=f"“{sentence}”", subtext=f"Your turn · {i} of {len(self.TRAINING)}")
                    audio = record_command(lambda: self.mic.read(timeout=1.0), self.cfg, self.detector,
                                           on_level=self.hud.level)
                    if audio is not None and len(audio) > 16000 * 0.6:
                        clips.append(audio)
                        texts.append(sentence)
                        self.chime("done")
                        break
                    self.say("I didn't hear that, Sir.", interruptible=False)
                if any(w in (self.transcripts[-1:] or [""])[0].lower() for w in ("stop", "cancel")):
                    break
        finally:
            self.cfg.listen.start_timeout_s, self.cfg.listen.max_command_s, self.cfg.listen.smart_end = old
        if len(clips) < 5:
            self.say("I didn't get enough to learn your voice, Sir. Say 'learn my voice' to try again.")
            return
        self.hud.show("thinking", text="", subtext="Learning your voice")
        try:
            info = self.voiceprint.enroll(clips)
        except ValueError:
            self.say("Those recordings were too quiet to learn from, Sir. Shall we try again later?")
            return
        settings.save(only_my_voice=True)
        safety.log("voice_trained", sentences=info["sentences"], consistency=info["consistency"])
        best = self._pick_hearing_model(clips, texts)
        del clips                       # the recordings are not kept
        line = "Done, Sir. I know your voice now, and I'll only listen to you."
        if best:
            line += f" I've also switched to the speech model that understood you best: {best}."
        self.say(line + " If I ever ignore you, say 'Hey Jarvis, learn my voice' again.")

    def _pick_hearing_model(self, clips: list, texts: list) -> str | None:
        """Try each installed speech model on your training sentences; keep the most accurate one that is
        still quick. Returns a description if it changed."""
        from .ears import make_transcriber, model_ready, use_model
        folder = self.cfg.hearing.model_dir.parent
        candidates = [d for d in sorted(folder.iterdir()) if model_ready(d)
                      and d.name not in ("faster-whisper-base.en", "faster-whisper-tiny.en")]

        def words(t):
            return re.findall(r"[a-z0-9']+", re.sub(r"hey,?\s*jarvis", "", t.lower()))

        def wer(ref, hyp):
            r, h = words(ref), words(hyp)
            d = list(range(len(h) + 1))
            for i in range(1, len(r) + 1):
                prev, d[0] = d[:], i
                for j in range(1, len(h) + 1):
                    d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (r[i - 1] != h[j - 1]))
            return d[-1] / max(1, len(r))

        results = []
        for d in candidates:
            try:
                run = self.hearing._run if d.name == self.hearing.model_name else \
                    make_transcriber(d, self.cfg.hearing.compute_type, self.cfg.hearing.cpu_threads)
                run(clips[0], "")
                t0 = time.monotonic()
                errs = [wer(ref, run(a, self.cfg.hearing.prompt)) for a, ref in zip(clips, texts)]
                secs = (time.monotonic() - t0) / len(clips)
                results.append((float(np.mean(errs)), secs, d))
                self.log(f"  (speech model {d.name}: {np.mean(errs) * 100:.0f}% words wrong, {secs:.2f}s each)")
            except Exception as e:
                safety.log_error(f"testing speech model {d.name}", e)
        quick = [r for r in results if r[1] <= self.cfg.hearing.max_seconds] or results
        if not quick:
            return None
        err, secs, best = min(quick, key=lambda r: (round(r[0], 3), r[1]))
        current = next((r for r in results if r[2].name == self.hearing.model_name), None)
        safety.log("hearing_model_tested", results={r[2].name: [round(r[0], 3), round(r[1], 2)] for r in results})
        if best.name == self.hearing.model_name or (current and current[0] <= err):
            return None
        use_model(self.hearing, best)
        settings.save(hearing_model=best.name)
        nice = best.name.replace("mlx-whisper-", "Whisper ").replace("faster-whisper-", "Whisper ").replace("-", " ")
        return f"{nice}, which got {100 - err * 100:.0f}% of your words right"

    def offer_voice_training(self):
        """Once, the very first time: what he can remember, then an offer to learn your voice."""
        prefs = settings.load()
        if prefs.get("intro_done") or prefs.get("voice_training_offered"):
            return
        settings.save(intro_done=True, voice_training_offered=True)
        self.say("Before we begin, Sir, two things. First, I remember what you tell me. Say, for example: "
                 "remember that my sister's birthday is March the third, and I'll keep it, here on this Mac. "
                 "Ask me what I remember whenever you like, or tell me to forget something.",
                 subtext="Memory")
        if self.voiceprint is None or self.voiceprint.trained:
            return
        if self.confirm("Second: shall I learn your voice, so that I only listen to you? It takes about a minute.",
                        "Learn your voice"):
            self.train_voice()
        else:
            self.say("Whenever you like, Sir, just say: Hey Jarvis, learn my voice.")

    def on_interruption(self, text: str) -> str | None:
        """You talked over him. "Stop" / "be quiet": he goes silent. "Undo" / "cancel": he reverses
        what he just did. Anything else is your next request (returned, to be handled)."""
        t = text.lower().strip(" .!?,")
        if not t or _HUSH.match(t):
            self.hud.show("listening", text="", subtext="Listening")
            return None
        if _UNDO.match(t):
            self._undo_said()
            return None
        return text

    def _undo_said(self):
        try:
            self.say(self.undo_last())
        except Interrupted as e:
            self.on_interruption(e.text)

    # ---------- undo ----------

    def _remember_undo(self, name: str, args: dict):
        """Before a skill runs, note how to reverse it, if it can be reversed."""
        undo = None
        try:
            if name == "volume":
                vol, muted_s = (skills.osa('set v to get volume settings\nreturn ((output volume of v) as text) & "|" & '
                                           '((output muted of v) as text)') + "|").split("|")[:2]
                prev, muted = int(vol or 0), muted_s == "true"
                undo = (f"Volume back to {prev} percent.", lambda: (skills.REGISTRY["volume"].fn(action="set", level=prev),
                                                                   skills.REGISTRY["volume"].fn(action="mute" if muted else "unmute")))
            elif name == "dark_mode":
                was = skills.osa('tell application "System Events" to tell appearance preferences to get dark mode') == "true"
                undo = ("Appearance back as it was.", lambda: skills.REGISTRY["dark_mode"].fn(mode="on" if was else "off"))
            elif name == "wifi":
                undo = (f"Wi-Fi back {'off' if args.get('state') == 'on' else 'on'}.",
                        lambda: skills.REGISTRY["wifi"].fn(state="off" if args.get("state") == "on" else "on"))
            elif name == "media":
                back = {"play": "pause", "pause": "play", "next": "previous", "previous": "next"}.get(args.get("action"))
                if back:
                    undo = ("Done.", lambda: skills.REGISTRY["media"].fn(action=back))
            elif name == "set_timer":
                undo = ("Timer cancelled.", lambda: skills.TIMERS.items and skills.TIMERS.items.pop())
            elif name == "remember":
                undo = ("I've forgotten that again.", lambda: skills.REGISTRY["forget"].fn(about=args.get("fact", "")))
            elif name == "remember_person":
                undo = (f"I've forgotten {args.get('name')}'s number.", lambda: skills.REGISTRY["forget_person"].fn(name=args.get("name", "")))
            elif name == "message_alerts":
                before = settings.load()
                undo = ("Message reading is back as it was.",
                        lambda: settings.save(read_new_messages=before["read_new_messages"], read_new_from=before["read_new_from"]))
            elif name == "open_app":
                undo = ("Closing it again.", lambda: self.run_skill("quit_app", {"name": args.get("name", "")}))
            elif name in ("create_note", "add_reminder", "add_event"):
                where = {"create_note": "Notes", "add_reminder": "Reminders", "add_event": "Calendar"}[name]
                undo = (f"I don't delete things, Sir, so please remove it in {where} yourself.", None)
            elif name in ("screenshot", "lock_screen", "power", "run_shortcut", "copy_to_clipboard"):
                undo = ("I'm afraid that one can't be undone, Sir.", None)
        except Exception:
            undo = None
        if undo:
            self.undo_stack = (self.undo_stack + [undo])[-10:]

    def undo_last(self) -> str:
        if not self.undo_stack:
            return "There's nothing for me to undo, Sir."
        label, fn = self.undo_stack.pop()
        if fn is None:
            return label
        try:
            fn()
            safety.log("undo")
            return f"Undone, Sir. {label}"
        except Exception:
            return "I couldn't undo that, Sir."

    def announce_due(self):
        """Say anything that has come due: a finished timer, a low battery, or (only if you turned it on)
        a new WhatsApp message. Talking over an announcement works like talking over anything else."""
        try:
            self._announce_due()
        except Interrupted as e:
            nxt = self.on_interruption(e.text)
            self.converse(nxt or "", opening=not nxt)

    def _announce_due(self):
        TIMERS.tick()
        self._read_new_messages()
        if time.monotonic() - self._battery_checked > 120:
            self._battery_checked = time.monotonic()
            try:
                warning = skills.battery_warning(self._battery_state)
            except Exception:
                warning = None
            if warning:
                safety.log("battery_warning")
                self.hud.show("speaking", text=warning, subtext="Battery")
                self.say(warning, subtext="Battery")
                self.hud.hide()
        while TIMERS.due_messages:
            msg = TIMERS.due_messages.pop(0)
            safety.log("timer_done")
            self.hud.show("speaking", text=msg, subtext="Timer")
            self.chime("done")
            self.say(msg, subtext="Timer")
            self.hud.hide()

    def _read_new_messages(self):
        """Reads new WhatsApp messages aloud, only while you've asked for that ("read new messages
        when they arrive"). Off by default. Never reads anything that arrived before it was turned on."""
        prefs = settings.load()
        if not prefs["read_new_messages"]:
            self._wa_last_pk = None
            return
        if time.monotonic() - self._wa_checked < self.cfg.messaging.check_every_s:
            return
        self._wa_checked = time.monotonic()
        try:
            wa = skills.whatsapp()
            if self._wa_last_pk is None:
                self._wa_last_pk = wa.newest_pk()      # start from now: nothing older is read out
                return
            new = wa.incoming_since(self._wa_last_pk)
        except Exception:
            return
        if not new:
            return
        self._wa_last_pk = max(m.pk for _, m in new)
        wanted = {wa_reader._norm(n) for n in prefs["read_new_from"]}
        by_chat: dict[int, list] = {}
        for chat, msg in new:
            if not wanted or wa_reader._norm(chat.name) in wanted:
                by_chat.setdefault(chat.pk, []).append((chat, msg))
        for items in by_chat.values():
            chat, last = items[-1]
            body = wa_reader.shown_text(last)
            if body.startswith("[this person sent a message telling Jarvis"):
                body = "[a message addressed to me, which I'm ignoring]"
            text = body if len(body) <= 220 else body[:220].rsplit(" ", 1)[0] + "…"
            spoken_text = text if not text.startswith("[") else f"sent {text.strip('[]')}"
            if chat.is_group:
                line = (f"In {chat.name}, {last.sender} says: {spoken_text}" if not text.startswith("[") else
                        f"In {chat.name}, {last.sender} {spoken_text}.")
            else:
                line = (f"New message from {chat.name}: {spoken_text}" if not text.startswith("[") else
                        f"{chat.name} {spoken_text}.")
            if len(items) > 1:
                line = f"{len(items)} new messages in {chat.name}. The latest: " + line.split(": ", 1)[-1] \
                    if ": " in line else f"{len(items)} new messages in {chat.name}. " + line
            safety.log("message_read_aloud", chat_kind="group" if chat.is_group else "person")
            self.hud.show("speaking", text=line[:160], subtext="WhatsApp")
            self.chime("done")
            self.say(line, subtext="WhatsApp")
        self.hud.hide()

    def warm_up(self):
        self.voice.warm(persona.ALL_STOCK + [persona.greeting()])
        self.brain.warm()

    # ---------- fn dictation ----------

    def start_dictation(self) -> bool:
        if self.dictation is None:
            from . import dictate
            if dictate.HELPER is None:
                return False          # tests: never the real keyboard
            self.dictation = Dictation(self)
        if self.dictation.helper.alive:
            return True
        return self.dictation.start()

    def stop_dictation(self):
        if self.dictation is not None:
            self.dictation.close()
            self.dictation = None

    def _dictate_now(self, recent: list):
        """fn was pressed while he was busy: drop that and dictate."""
        self.hud.hide()
        self.end_conversation = True
        if self.dictation is not None:
            try:
                self.dictation.take(recent)
            except Exception as e:
                safety.log_error("dictation", e)
                self.dictation.helper.pill("hidden")

    def dictation_setup_note(self, force: bool = False) -> str:
        """What still needs doing for fn dictation, in words ("" when it's all set)."""
        if self.dictation is None:
            return ""
        end = time.monotonic() + 2
        while time.monotonic() < end and not (self.dictation.helper.ready or self.dictation.helper.error):
            time.sleep(0.1)
        st = self.dictation.status()
        todo = []
        if not st["fn_key"] or st["error"] == "no-input-monitoring":
            todo.append("allow Jarvis under Input Monitoring, in System Settings, Privacy and Security")
        if not st["accessibility"]:
            todo.append("allow Jarvis under Accessibility, in System Settings, Privacy and Security")
        if not st["globe_do_nothing"]:
            todo.append("set 'Press globe key to' to 'Do Nothing', in System Settings, Keyboard")
        if not todo:
            return ""
        return "To dictate with the fn key, Sir, please " + "; and ".join(todo) + ". Then restart me."

    def _tell_dictation_setup_once(self):
        if self.dictation is None or settings.load().get("dictation_setup_told"):
            return
        note = self.dictation_setup_note()
        settings.save(dictation_setup_told=True)
        if note:
            self.log("  (fn dictation setup: " + note + ")")
            self.say("One thing, Sir: you can now hold the fn key to dictate into any text box. " + note,
                     subtext="fn dictation")
            self.hud.hide()

    def run(self, greet: bool = True):
        self.warm_up()
        if settings.load().get("dictation", True):
            self.start_dictation()
        if greet:
            try:
                try:
                    self.say(persona.greeting(), subtext="Online")
                    self.offer_voice_training()
                    self._tell_dictation_setup_once()
                    self.converse("", opening=True)   # he waits a few seconds after greeting you
                except Interrupted as e:
                    self.converse(self.on_interruption(e.text) or "")
            except DictationRequested:
                self._dictate_now([])
        self.log("Listening for “Hey Jarvis”…  (Ctrl+C to stop)")
        recent: list[np.ndarray] = []   # the last two seconds, for the second check
        zero_frames, warned_mic = 0, False
        while self.running:
            try:
                recent, stop, frame = self._loop_once(recent)
                if stop:
                    break
                if frame is not None:
                    # macOS hands over pure silence when the microphone isn't allowed.
                    zero_frames = zero_frames + 1 if not frame.any() else 0
                    if zero_frames == 50 and not warned_mic:
                        warned_mic = True
                        self.log("!! The microphone is sending pure silence. Allow it in System Settings > "
                                 "Privacy & Security > Microphone, for Jarvis, then start Jarvis again.")
                        self.say("I can't hear anything, Sir. Please allow microphone access for Jarvis "
                                 "in System Settings, then start me again.", state="error", subtext="Microphone blocked")
                        self.hud.hide()
            except DictationRequested:
                self._dictate_now(recent)
                recent = []

    def _loop_once(self, recent: list) -> tuple[list, bool, np.ndarray | None]:
        """One turn of the idle loop: announcements, fn dictation, and listening for "Hey Jarvis"."""
        try:
            self.announce_due()
        except Exception as e:
            safety.log_error("announcements", e)
        self._check_audio()
        if self.dictation is not None:
            self.dictation.check()
            if self.dictation.requested():
                self._dictate_now(recent)
                return [], False, None
        frame = self.mic.read(timeout=1.0)
        if frame is None:
            return recent, bool(getattr(self.mic, "finished", False)), None
        recent = (recent + [frame])[-25:]
        score = self.wake.hear(frame)
        if score:
            try:
                self.on_wake(score, recent)
            except Exception as e:
                safety.log_error("after the wake word", e)
                self.hud.hide()
            recent = []
        return recent, False, frame

    def _check_audio(self):
        """If the echo-cancelling audio helper has died, start it again instead of going deaf."""
        mic = self.mic
        if not hasattr(mic, "alive") or mic.alive:
            return
        if time.monotonic() - getattr(self, "_audio_restarted", 0) < 10:
            return
        self._audio_restarted = time.monotonic()
        safety.log("audio_restart", why=getattr(mic, "failed", ""))
        mic.stop()
        mic.__init__(self.cfg.frame_samples)
        if mic.start():
            self.voice.output = mic

    def close(self):
        self.stop_dictation()
        self.hud.close()


# Names speech recognition tends to misspell unless told to expect them.
POPULAR_ARTISTS = ["Arijit Singh", "Shreya Ghoshal", "Atif Aslam", "A. R. Rahman", "Pritam", "Badshah", "Diljit Dosanjh",
                   "AP Dhillon", "Jubin Nautiyal", "Neha Kakkar", "Sonu Nigam", "Kishore Kumar", "Lata Mangeshkar",
                   "Karan Aujla", "Anuv Jain", "Prateek Kuhad", "Sachet Tandon", "Vishal Mishra", "Taylor Swift",
                   "Ed Sheeran", "The Weeknd", "Billie Eilish", "Drake"]


def split_at_pauses(audio: np.ndarray, min_pause: float = 0.3) -> list[np.ndarray]:
    """Cut audio into stretches of speech wherever there is a pause of at least `min_pause` seconds."""
    frame = 320
    n = len(audio) // frame
    if n < 10:
        return [audio]
    e = np.sqrt(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1))
    quiet = e < max(0.004, np.percentile(e, 90) * 0.1)
    parts, start, run = [], None, 0
    need = int(min_pause * 16000 / frame)
    for i, q in enumerate(quiet):
        if not q:
            if start is None:
                start = i
            run = 0
        elif start is not None:
            run += 1
            if run >= need:
                parts.append(audio[start * frame:(i - run + 2) * frame])
                start, run = None, 0
    if start is not None:
        parts.append(audio[start * frame:])
    return [p for p in parts if len(p) > frame * 5] or [audio]


def music_is_playing() -> bool:
    try:
        return skills.app_running("Music") and skills.osa('tell application "Music" to return player state as text',
                                                           timeout=5) == "playing"
    except Exception:
        return False


_NUM_WORDS = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
              "sixty": 60, "a few": 10, "a bit": 10, "a little": 10, "a minute": 60, "one minute": 60}


def _seconds(text: str) -> int:
    from .timeparse import duration_seconds
    d = duration_seconds(text)
    if d:
        return d
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    for w, n in sorted(_NUM_WORDS.items(), key=lambda kv: -len(kv[0])):
        if w in text:
            return n
    return 10


def music_command(t: str) -> tuple[str, dict] | None:
    """Recognise everyday music commands without the brain. t is lower-case, without end punctuation."""
    t = re.sub(r"^(?:(?:ok|okay|now|so|and|hey)[, ]+)+", "", t).strip()
    M = lambda pat: re.match(pat, t)
    # pause / stop / resume
    if M(r"^(pause|stop|halt)( the| this| my)?( music| song| track| it| playback| playing| audio)?$|^stop playing( music)?$|"
         r"^(pause|stop) (the )?(music|song)( for a (moment|bit|second))?$|^(turn|switch) (off|the music off) the music$|^music off$"):
        return "media", {"action": "pause" if not t.startswith(("turn", "switch", "music off")) else "pause"}
    if M(r"^(resume|unpause|continue|carry on)( the)?( music| song| playing| it)?$|^play( the)?( music| song| it)?( again)? ?(now)?$|"
         r"^(start|turn on|put on) (the )?music( again)?$|^music on$|^play some music$|^play music$"):
        return ("media", {"action": "restart"}) if t.endswith("again") and "music" not in t else ("media", {"action": "play"})
    # next / previous / restart
    if M(r"^(next|skip)( song| track| this( song| one)?| it| one)?$|^(play )?(the )?next (song|track|one)$|^change (the )?song$|^another song$"):
        return "media", {"action": "next"}
    if M(r"^(previous|back)( song| track)?$|^go back( a song| a track)?$|^(play )?(the )?(last|previous) (song|track|one)( again)?$"):
        return "media", {"action": "previous"}
    if M(r"^(play|start) (it|this|this song|that|that song|the song) (again|over)$|^restart (the |this )?song$|^from the (top|start|beginning)$|"
         r"^start (it|the song) from the (beginning|start)$|^play (it|this) from the (start|beginning)$|^replay( this| the song)?$"):
        return "media", {"action": "restart"}
    # about the song
    if M(r"^(what'?s|what is) (playing|this song|this|the song)( called)?$|^what song is (this|playing)$|^(name of )?this song$|^which song is this$"):
        return "song_info", {"what": "all"}
    if M(r"^who (sings|sang|is singing|'s singing) (this|it)( song)?$|^who'?s (singing|this)$|^who is this( singer| artist)?$|^which artist is this$"):
        return "song_info", {"what": "artist"}
    if M(r"^(which|what) album is (this|it)( from)?$|^what album$"):
        return "song_info", {"what": "album"}
    if M(r"^how (long|much) (is )?left( in| of)?( this| the)?( song)?$|^how much (of the song )?is left$"):
        return "song_info", {"what": "time_left"}
    # shuffle / repeat
    if M(r"^shuffle (my )?(library|music|songs|all( my)? songs)$|^play (something|anything) from my library$|^play my (library|music)$"):
        return "play_music", {"query": "my library"}
    if M(r"^(turn |switch )?(on )?shuffle( on)?( mode)?$|^shuffle (it|this|the songs|the music)$"):
        return "music_mode", {"shuffle": "on"}
    if M(r"^(turn |switch )?(off )?shuffle off$|^(turn|switch) off shuffle$|^stop shuffling$|^no shuffle$|^shuffle off$"):
        return "music_mode", {"shuffle": "off"}
    if M(r"^(repeat|loop) (this|this song|the song|it|this one)$|^(play|put) (this|it) on repeat$|^on repeat$|^repeat( one)?$"):
        return "music_mode", {"repeat": "one"}
    if M(r"^repeat (all|everything|the (playlist|album))$"):
        return "music_mode", {"repeat": "all"}
    if M(r"^(turn |switch )?(off|stop) (the )?repeat(ing)?$|^(turn |switch )?repeat off$|^no (more )?repeat$|^stop looping$"):
        return "music_mode", {"repeat": "off"}
    # skip within the song
    m = M(r"^(?:skip|go|jump|fast forward|move)? ?(?:forward|ahead|fast forward)(?: by)? (.+?)(?: seconds?| secs?)?$")
    if m and not re.search(r"song|track", t):
        return "music_seek", {"seconds": _seconds(m.group(1))}
    m = M(r"^(?:go |skip |jump |rewind |move )?back(?:ward|wards)?(?: by)? (.+?) (?:seconds?|secs?)$|^rewind(?: by)? (.+?)(?: seconds?)?$")
    if m:
        return "music_seek", {"seconds": -_seconds(m.group(1) or m.group(2))}
    # the music's own volume
    m = M(r"^(?:set )?(?:the )?music(?: volume)?(?: to)? (\d{1,3})(?: percent)?$")
    if m:
        return "music_volume", {"action": "set", "level": int(m.group(1))}
    if M(r"^(turn )?(the )?music (up|louder)$|^(turn up|louder) (the )?music$|^make (the )?music louder$"):
        return "music_volume", {"action": "up"}
    if M(r"^(turn )?(the )?music (down|quieter|softer)$|^(turn down|lower) (the )?music$|^make (the )?music (quieter|softer)$"):
        return "music_volume", {"action": "down"}
    # love, playlists
    if M(r"^(i )?(love|like) (this|this song|it|this one)$|^(add (this|it|this song) to|save (this|it) (to|in)) (my )?(favou?rites|loved songs)$|"
         r"^favou?rite (this|it)( song)?$|^(love|heart) (this|it)$"):
        return "love_song", {"state": "love"}
    if M(r"^(unlove|unlike|un ?favou?rite|remove (the )?love from)( this| it| this song)?$|^i don'?t (love|like) this( song)?$"):
        return "love_song", {"state": "unlove"}
    m = M(r"^(?:add|put|save) (?:this|it|this song|the song)(?: song)? (?:to|in|into) (?:my |the )?(.+?)(?: playlist)?$")
    if m:
        return "add_to_playlist", {"playlist": m.group(1).strip()}
    if M(r"^(what|which) playlists do i have$|^(list|show|tell me)( me)? my playlists$|^my playlists$"):
        return "list_playlists", {}
    if M(r"^(play )?more (like this|like that|songs like this)$|^more (songs )?(from|by) (this|the same) (artist|singer)$|"
         r"^play more (of|from|by) (this|the same) (artist|singer)$"):
        return "play_similar", {}
    # sleep timer
    m = M(r"^(?:stop|pause|turn off|switch off) (?:the )?music (?:in|after) (.+)$|^(?:set a )?(?:music )?sleep timer (?:for )?(.+)$")
    if m:
        return "music_sleep_timer", {"duration": m.group(1) or m.group(2)}
    # AirPlay
    if M(r"^(which|what) (speakers|airplay devices|devices) (do i have|are there|can you (see|play on))$|^list (my )?speakers$"):
        return "airplay", {"device": "list"}
    m = M(r"^(?:play|switch|move|send) (?:it|this|the music|music|audio|the audio|the song) (?:on|to|through) (?:the |my )?(.+?)$")
    if m and not re.search(r"apple music|spotify|youtube", m.group(1)):
        return "airplay", {"device": m.group(1)}
    # play <something>
    m = M(r"^(?:please )?(?:play|put on|start playing|i want to (?:hear|listen to)|let'?s (?:hear|listen to))(?: me)? "
          r"(?P<pre>the song |a song |song |the album |album |the playlist |playlist |my |some songs by |songs by |music by |"
          r"something by |some music by |some |the )?(?P<q>.+?)(?: (?:songs?|music))?(?: on apple music| in apple music| in music| please)?$")
    if m:
        q, pre = m.group("q").strip(), (m.group("pre") or "").strip()
        if q in ("music", "something", "anything", "a song", "songs", "it", "this", "that", "some"):
            return "media", {"action": "play"}
        kind = ("album" if "album" in pre else "playlist" if "playlist" in pre or t.endswith("playlist") else
                "artist" if re.search(r"by$", pre) or re.search(r" songs?$", t) else "any")
        q = re.sub(r"\s+playlist$", "", q)
        return "play_music", {"query": q, "kind": kind}
    return None


def _original(text: str, lowered_part: str) -> str:
    """The same words as they were said (capitals kept), found in the original text."""
    at = text.lower().find(lowered_part)
    return text[at:at + len(lowered_part)].strip() if at >= 0 else lowered_part


def about_sir(text: str) -> str:
    """Rewrite "my sister's birthday is March 3rd" as "Sir's sister's birthday is March 3rd"."""
    swaps = [(r"\bI am\b", "Sir is"), (r"\bI'm\b", "Sir is"), (r"\bI've\b", "Sir has"), (r"\bI\b", "Sir"),
             (r"\bmy\b", "Sir's"), (r"\bme\b", "Sir"), (r"\bmine\b", "Sir's"), (r"\bmyself\b", "Sir")]
    out = text.strip().rstrip(".")
    for pat, rep in swaps:
        out = re.sub(pat, rep, out, flags=re.I)
    # "Sir like" -> "Sir likes", "Sir have" -> "Sir has"
    out = re.sub(r"\bSir (have|do|go|study)\b", lambda m: "Sir " + {"have": "has", "do": "does", "go": "goes",
                                                                      "study": "studies"}[m.group(1).lower()], out)
    out = re.sub(r"\bSir (like|love|hate|prefer|want|need|live|work|use|drink|eat|play|own|drive|wake|sleep|"
                 r"support|follow|take|watch|listen|read|speak|enjoy|dislike|believe|think|feel|run|wear)\b",
                 lambda m: f"Sir {m.group(1)}s", out)
    if out.startswith("Sir ") or out.lower().startswith("i "):
        out = out[:4] + re.sub(r"\bSir's\b", "his", out[4:])     # "Sir likes his tea strong"
    return out[:1].upper() + out[1:]


def difflib_close(said: str, app_title: str) -> bool:
    """Only open an app instantly when the name clearly matches; otherwise let the brain decide."""
    import difflib
    s = re.sub(r"\b(the|app)\b", "", said.lower()).strip()
    a = app_title.lower()
    return s == a or a.startswith(s) or s in skills._ALIASES or difflib.SequenceMatcher(None, s, a).ratio() >= 0.85
