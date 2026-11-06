"""Turns spoken times into real ones: "in 10 minutes", "tomorrow at 9", "5:30 pm".

Done in plain code, not by the AI model, because small models are unreliable at
date arithmetic.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
        "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5,
        "half a": 0.5, "a couple of": 2, "couple of": 2, "few": 3}
_UNIT = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _num(s: str) -> float | None:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        return _NUM.get(s)


def duration_seconds(text: str) -> int | None:
    """'10 minutes', 'an hour and a half', '1 hour 20 minutes', '90 seconds' -> seconds."""
    t = text.lower().replace("-", " ")
    t = re.sub(r"\band a half\b", "and 0.5", t)
    total = 0.0
    found = False
    for m in re.finditer(r"(\d+(?:\.\d+)?|half an|half a|a couple of|couple of|forty five|[a-z]+)\s*"
                         r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)\b", t):
        n = _num(m.group(1))
        if n is None:
            continue
        unit = re.sub(r"s$", "", m.group(2))
        total += n * _UNIT[unit]
        found = True
    m = re.search(r"(\w+) (hours?|minutes?) and 0\.5", t)
    if m and found:
        total += 0.5 * _UNIT[re.sub(r"s$", "", m.group(2))]
    return int(total) if found and total > 0 else None


def _clock(text: str) -> tuple[int, int] | None:
    t = text.lower().replace(".", "")
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return 12, 0
    if re.search(r"\bmidnight\b", t):
        return 0, 0
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a m|p m)?\b(?!\s*(?:minutes?|mins?|hours?|days?|seconds?))", t)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(" ", "")
    if h > 23 or mi > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    elif not ap and h <= 7 and not re.search(r"morning", t):
        h += 12  # "at 5" usually means 5 pm
    if re.search(r"evening|tonight|afternoon", t) and h < 12:
        h += 12
    return h, mi


def parse_when(text: str | None, now: datetime | None = None) -> datetime | None:
    """A moment in the future, or None if it can't be read."""
    if not text:
        return None
    now = now or datetime.now()
    t = text.lower().strip()

    # ISO-ish: 2026-11-07 09:00
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})(?:[ t](\d{1,2}):(\d{2}))?", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = (int(m.group(4)), int(m.group(5))) if m.group(4) else (9, 0)
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None

    # Relative: "in 10 minutes", "in an hour"
    if re.search(r"\bin\b|\bafter\b|from now", t):
        secs = duration_seconds(t)
        if secs:
            return now + timedelta(seconds=secs)

    day = now.date()
    if "day after tomorrow" in t:
        day = day + timedelta(days=2)
    elif "tomorrow" in t:
        day = day + timedelta(days=1)
    else:
        for i, name in enumerate(_DAYS):
            if re.search(rf"\b{name}\b", t):
                ahead = (i - now.weekday()) % 7
                if ahead == 0 and not re.search(r"\bnext\b", t):
                    c = _clock(t)
                    if not c or (c[0], c[1]) <= (now.hour, now.minute):
                        ahead = 7    # that time has passed today: next week
                elif ahead == 0:
                    ahead = 7
                day = day + timedelta(days=ahead)
                break

    clock = _clock(t)
    if clock is None:
        if re.search(r"\bmorning\b", t):
            clock = (9, 0)
        elif re.search(r"\bafternoon\b", t):
            clock = (14, 0)
        elif re.search(r"\bevening\b|\btonight\b", t):
            clock = (19, 0)
        elif day != now.date():
            clock = (9, 0)
        else:
            return None
    when = datetime.combine(day, datetime.min.time()).replace(hour=clock[0], minute=clock[1])
    if when <= now and day == now.date():
        when += timedelta(days=1)  # "at 9" when it's already 10 means tomorrow
    return when


def say_when(when: datetime, now: datetime | None = None) -> str:
    """How Jarvis says a moment: 'at 5:30 this evening', 'tomorrow at 9 in the morning'."""
    now = now or datetime.now()
    h = when.hour % 12 or 12
    clock = f"{h}" if when.minute == 0 else f"{h}:{when.minute:02d}"
    part = ("in the morning" if when.hour < 12 else "in the afternoon" if when.hour < 17 else "in the evening")
    if (when.hour, when.minute) == (12, 0):
        clock, part = "noon", ""
    elif (when.hour, when.minute) == (0, 0):
        clock, part = "midnight", ""
    if when.date() == now.date():
        part = {"in the morning": "this morning", "in the afternoon": "this afternoon",
                "in the evening": "this evening", "": "today"}[part]
        return f"at {clock} {part}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock} {part}".strip()
    if (when.date() - now.date()).days < 7:
        return f"on {when.strftime('%A')} at {clock} {part}".strip()
    d = when.day
    th = "th" if 11 <= d <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d % 10, "th")
    return f"on {when.strftime('%A')} the {d}{th} of {when.strftime('%B')} at {clock} {part}".strip()


def say_duration(seconds: int) -> str:
    seconds = int(round(seconds))
    parts = []
    for name, size in (("hour", 3600), ("minute", 60), ("second", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " and ".join(parts[:2]) or "0 seconds"
