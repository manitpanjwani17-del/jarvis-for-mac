"""How Jarvis speaks: a composed, dryly polite British butler who calls you Sir."""
from __future__ import annotations

import random
import re
import time

WAKE = ["Yes, Sir?", "Sir?", "At your service, Sir.", "Listening, Sir.", "How may I help, Sir?"]
ON_IT = ["Okay, Sir. On it.", "Right away, Sir.", "Very good, Sir.", "Consider it done, Sir."]
DIDNT_CATCH = ["I'm afraid I didn't catch that, Sir.", "Apologies, Sir, I missed that."]
NOTHING_SAID = []  # if you say nothing after waking it, Jarvis simply goes quiet
CANCELLED = ["Very well, Sir. Cancelled.", "As you wish, Sir. I won't."]
NO_ANSWER = ["I'll take that as a no, Sir."]
STAND_BY = ["Standing by, Sir.", "Of course, Sir."]
GOODBYE = ["Powering down. Good night, Sir.", "Going offline, Sir. Call if you need me."]

ALL_STOCK = WAKE + ON_IT + DIDNT_CATCH + CANCELLED + NO_ANSWER + STAND_BY + GOODBYE + [
    "Shall I go offline, Sir?",
]


def pick(lines: list[str], **fmt) -> str:
    return random.choice(lines).format(**fmt)


def greeting() -> str:
    h = time.localtime().tm_hour
    part = "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening"
    return f"Good {part}, Sir. Jarvis is online."


def time_now() -> str:
    t = time.localtime()
    hour = t.tm_hour % 12 or 12
    minute = t.tm_min
    when = "in the morning" if t.tm_hour < 12 else "in the afternoon" if t.tm_hour < 17 else "in the evening"
    spoken = f"{hour} o'clock" if minute == 0 else f"{hour}:{minute:02d}"
    return f"It's {spoken} {when}, Sir."


def date_today() -> str:
    d = time.localtime().tm_mday
    suffix = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return time.strftime(f"Today is %A, the {d}{suffix} of %B, Sir.")


def addressed(text: str) -> str:
    """Jarvis's lines say "Sir"; use what you chose at setup instead ("Ma'am", or your name)."""
    if not text or "Sir" not in text:
        return text
    from . import settings
    who = (settings.load().get("address") or "Sir").strip()
    return text if who == "Sir" else re.sub(r"\bSir\b", who, text)
