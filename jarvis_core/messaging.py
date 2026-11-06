"""Sending messages: WhatsApp and iMessage, to people in your Contacts.

Nothing here sends on its own. assistant.py reads the message back and waits for a
spoken yes before calling send(). This module finds the person, checks the
conditions, and does the sending.

Privacy: Jarvis never reads your chats here. The WhatsApp helper only checks that the
message box holds exactly the approved words before pressing Enter.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DATA, HELPERS
HELPER = HELPERS / "JarvisMessenger"
ALIASES_FILE = DATA / "contact_aliases.json"
PEOPLE_FILE = DATA / "people.json"     # names and numbers you told Jarvis (not your Contacts)

# Family words people use for the same person. "Mum" also finds a contact saved as "Mummy" or "Maa".
FAMILY = [
    {"mum", "mom", "mummy", "mommy", "mother", "maa", "ma", "mamma", "mumma", "amma", "ammi", "mata"},
    {"dad", "daddy", "papa", "pappa", "father", "pa", "pops", "pitaji", "abba", "baba"},
    {"bro", "brother", "bhai", "bhaiya"},
    {"sis", "sister", "didi", "behen"},
    {"grandma", "granny", "nani", "dadi"},
    {"grandpa", "nana", "dada"},
]


@dataclass
class Contact:
    name: str
    phones: list[dict]            # [{"label": "mobile", "number": "+91 98…"}]

    def best_number(self) -> str | None:
        order = ["whatsapp", "mobile", "iphone", "cell", "main", "home", "work"]
        ranked = sorted(self.phones, key=lambda p: next((i for i, k in enumerate(order) if k in p["label"].lower()), 99))
        return ranked[0]["number"] if ranked else None


class MessagingError(Exception):
    """Something stopped the message. `spoken` is what Jarvis says about it."""

    def __init__(self, code: str, spoken: str):
        super().__init__(code)
        self.code = code
        self.spoken = spoken


# ---------------------------------------------------------------- phone numbers

def your_country_code() -> str:
    """The code you chose at setup or by saying "my country code is 44" ("" if not set yet)."""
    from . import settings
    return str(settings.load().get("country_code") or "").lstrip("+")


def whatsapp_digits(number: str, country_code: str | None = None) -> str:
    """'+44 7700 900123' / '07700 900123' / '7700 900123' -> '447700900123' (what WhatsApp's link wants).
    A number said without its country code gets yours (set at setup, or "my country code is 44")."""
    if country_code is None:
        country_code = your_country_code()
    raw = number.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return digits
    if digits.startswith("00"):
        return digits[2:]
    if len(digits) > 10 and not digits.startswith("0"):
        return digits                      # already has its country code
    if digits.startswith("0"):
        return country_code + digits[1:]   # the local leading 0 is dropped
    return country_code + digits


def e164(number: str, country_code: str | None = None) -> str:
    return "+" + whatsapp_digits(number, country_code)


# ---------------------------------------------------------------- the helper

def _helper(*args: str, timeout: float = 60) -> object:
    if not HELPER.exists():
        raise MessagingError("helper-missing", "My messaging module is missing, Sir.")
    p = subprocess.run([str(HELPER), *args], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except ValueError:
        return {"error": "bad-output"}


def screen_locked() -> bool:
    out = _helper("locked", timeout=10)
    return bool(isinstance(out, dict) and out.get("locked"))


def accessibility_allowed(prompt: bool = False) -> bool:
    out = _helper("trusted", *(["--prompt"] if prompt else []), timeout=10)
    return bool(isinstance(out, dict) and out.get("trusted"))


# ---------------------------------------------------------------- finding people

def load_aliases() -> dict[str, str]:
    try:
        return json.loads(ALIASES_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_alias(said: str, contact_name: str):
    aliases = load_aliases()
    aliases[said.lower().strip()] = contact_name
    ALIASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALIASES_FILE.write_text(json.dumps(aliases, indent=1, ensure_ascii=False))


def _clean_name(said: str) -> str:
    s = said.lower().strip(" .?!,")
    s = re.sub(r"^(my|the|to)\s+", "", s)
    s = re.sub(r"'s$", "", s)
    return s


def _real_lookup(words: list[str]) -> object:
    return _helper("contacts", *words)


CONTACTS_LOOKUP = _real_lookup   # tests replace this, so they never read your real Contacts


def search_contacts(words: list[str], lookup=None) -> list[Contact]:
    lookup = lookup or CONTACTS_LOOKUP
    out = lookup(words)
    if isinstance(out, dict) and out.get("error") == "no-permission":
        raise MessagingError("no-contacts-permission",
                             "I need permission to look in your Contacts, Sir. Please allow Terminal in System "
                             "Settings, Privacy and Security, Contacts.")
    if not isinstance(out, list):
        raise MessagingError("contacts-failed", "Contacts didn't respond, Sir.")
    return [Contact(c["name"], c["phones"]) for c in out if c.get("phones")]


def find_contacts(said: str, lookup=None) -> list[Contact]:
    """Everyone in Contacts who could be meant by `said`, best guesses first."""
    name = _clean_name(said)
    aliases = load_aliases()
    if name in aliases:
        exact = [c for c in search_contacts(aliases[name].split(), lookup) if c.name.lower() == aliases[name].lower()]
        if exact:
            return exact
    found: dict[str, Contact] = {}
    for c in search_contacts(name.split(), lookup):
        found.setdefault(c.name, c)
    if not found:
        for group in FAMILY:                 # "Mum" -> also try "Mummy", "Maa", "Mother"…
            if name in group:
                for other in sorted(group - {name}):
                    for c in search_contacts([other], lookup):
                        found.setdefault(c.name, c)
    # Exact name first, then shortest (most specific) names.
    return sorted(found.values(), key=lambda c: (c.name.lower() != name, len(c.name)))


# ---------------------------------------------------------------- Jarvis's own people book

def load_people() -> dict[str, dict]:
    try:
        return json.loads(PEOPLE_FILE.read_text())
    except (OSError, ValueError):
        return _people_from_memory()


def _people_from_memory() -> dict[str, dict]:
    """Once, when there's no people book yet: numbers you gave Jarvis before he had one were kept as
    memory notes ("MIMMY: +65…"). Move them into the people book so he can message them."""
    try:
        from . import skills
        notes = [i["fact"] for i in skills.load_memory()]
    except Exception:
        return {}
    people = {}
    for fact in notes:
        m = re.match(r"^\s*(?:Sir's\s+)?([A-Za-z][\w .'-]{0,40}?)(?:'s)?(?:\s+(?:phone\s+)?number(?:\s+is)?)?\s*[:=-]?\s*(\+?[\d][\d\s-]{6,})\s*$", fact)
        if m:
            name, digits = m.group(1).strip(), spoken_number(m.group(2))
            if digits:
                people[name.lower()] = {"name": name, "number": digits if digits.startswith("+") else e164(digits)}
    if people:
        PEOPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PEOPLE_FILE.write_text(json.dumps(people, indent=1, ensure_ascii=False))
    return people


def remember_person(name: str, number: str) -> str:
    """Keep a name and number in Jarvis's memory. Returns the number as stored (+country code)."""
    people = load_people()
    stored = e164(number)
    people[name.strip().lower()] = {"name": name.strip().title() if name.islower() else name.strip(), "number": stored}
    PEOPLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PEOPLE_FILE.write_text(json.dumps(people, indent=1, ensure_ascii=False))
    return stored


def forget_person(name: str) -> bool:
    people = load_people()
    key = name.strip().lower()
    if key not in people:
        return False
    del people[key]
    PEOPLE_FILE.write_text(json.dumps(people, indent=1, ensure_ascii=False))
    return True


# ---------------------------------------------------------------- spoken numbers

_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def spoken_number(text: str) -> str | None:
    """'98200 12345', '+91 98200-12345', 'nine eight two zero zero…', 'double five' -> digits (with + if said)."""
    t = text.lower().replace("plus", "+")
    t = re.sub(r"\b(double|triple)\s+(\w+)", lambda m: " ".join([m.group(2)] * (2 if m.group(1) == "double" else 3)), t)
    words = re.findall(r"\+|\d+|[a-z]+", t)
    out = ""
    for w in words:
        if w == "+" and not out:
            out = "+"
        elif w.isdigit():
            out += w
        elif w in _DIGIT_WORDS:
            out += _DIGIT_WORDS[w]
        elif out and w not in ("dash", "hyphen", "space"):
            break
    digits = out.lstrip("+")
    return out if 5 <= len(digits) <= 15 else None


def number_problem(number: str, country_code: str | None = None) -> str | None:
    """None if the number looks complete; otherwise what Jarvis says."""
    if country_code is None:
        country_code = your_country_code()
    digits = re.sub(r"\D", "", number)
    if number.strip().startswith("+") or (len(digits) > 10 and not digits.startswith("0")):
        return None if 8 <= len(digits) <= 15 else "That number doesn't sound complete, Sir."
    if not country_code:
        return ("I don't know your country code yet, Sir. Say, for example: my country code is 44. "
                "Or say the number with its country code.")
    local = digits[1:] if digits.startswith("0") else digits
    if country_code == "91":
        if len(local) != 10:
            return (f"I only heard {len(local)} digits, Sir: {say_digits(local)}. A mobile number has 10. "
                    f"Could you say it again, slowly?")
        if local[0] not in "6789":
            return f"That doesn't look like an Indian mobile number, Sir: {say_digits(local)}. Could you say it again?"
        return None
    if not 6 <= len(local) <= 11:
        return f"I only heard {len(local)} digits, Sir: {say_digits(local)}. Could you say it again, slowly?"
    return None


def say_digits(number: str) -> str:
    """'+919820012345' -> 'plus nine one, nine eight two zero zero, one two three four five'."""
    names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    digits = re.sub(r"\D", "", number)
    groups = []
    if number.startswith("+") and len(digits) > 10:
        cc, digits = digits[:-10], digits[-10:]
        groups.append("plus " + " ".join(names[int(d)] for d in cc))
    size = 5 if len(digits) == 10 else 4
    for i in range(0, len(digits), size):
        groups.append(" ".join(names[int(d)] for d in digits[i:i + size]))
    if number.startswith("+") and not groups[0].startswith("plus"):
        groups[0] = "plus " + groups[0]
    return ", ".join(groups)


# ---------------------------------------------------------------- who "to" means

@dataclass
class Target:
    name: str
    kind: str = "person"            # "person" or "group"
    number: str | None = None       # for people with a known number
    chat: str | None = None         # WhatsApp chat name, for groups and people shown without a number
    source: str = ""                # "spoken", "book", "whatsapp", "contacts"


def resolve(to: str, whatsapp=None, lookup=None) -> list[Target]:
    """Everyone and every chat `to` could mean, best first.

    Looks, in order: a number you said, your nicknames, Jarvis's people book, your WhatsApp chats
    (groups, and people you've never saved), then your Contacts.
    """
    said = to.strip()
    num = spoken_number(said)
    if num:
        return [Target(name=num, number=num if num.startswith("+") else e164(num), source="spoken")]
    name = _clean_name(said)
    aliases = load_aliases()
    for key in (name, re.sub(r"^my ", "", name)):
        if key in aliases:
            name = _clean_name(aliases[key])
            break

    found: list[Target] = []
    seen_numbers: set[str] = set()
    seen_chats: set[str] = set()

    def add(t: Target):
        digits = re.sub(r"\D", "", t.number or "")
        if digits and digits in seen_numbers:
            return
        if t.chat and t.chat.lower() in seen_chats:
            return
        if digits:
            seen_numbers.add(digits)
        if t.chat:
            seen_chats.add(t.chat.lower())
        found.append(t)

    book = load_people()
    for key, p in book.items():
        if key == name or key.startswith(name) or name in key.split():
            add(Target(p["name"], number=p["number"], source="book"))

    book_names = {t.name.lower() for t in found}
    if whatsapp is not None:
        try:
            for c in whatsapp.find_chats(name):
                if not c.is_group and not c.phone and c.name.lower() in book_names:
                    continue   # you gave Jarvis this person's number: that's who you mean
                if c.is_group:
                    add(Target(c.name, kind="group", chat=c.name, source="whatsapp"))
                elif c.phone:
                    add(Target(c.name, number="+" + c.phone, chat=c.name, source="whatsapp"))
                else:
                    add(Target(c.name, chat=c.name, source="whatsapp"))
        except Exception:
            pass   # WhatsApp unreadable: fall back to Contacts

    try:
        for c in find_contacts(name, lookup):
            n = c.best_number()
            if n:
                add(Target(c.name, number=e164(n), source="contacts"))
    except MessagingError:
        if not found:
            raise

    found = _merge_same_names(found)
    exact = [t for t in found if t.name.lower() == name]
    return exact + [t for t in found if t not in exact]


def _merge_same_names(targets: list[Target]) -> list[Target]:
    """One person, many places: "My Papa" in WhatsApp and "My Papa" in Contacts is the same person, so
    there's nothing to ask. WhatsApp's entry leads (you message mostly on WhatsApp); a number from any
    source fills in if WhatsApp's chat has none. Groups are never merged with people."""
    merged: dict[tuple, Target] = {}
    order: list[tuple] = []
    rank = {"whatsapp": 0, "book": 1, "contacts": 2, "spoken": 3}
    for t in sorted(targets, key=lambda t: rank.get(t.source, 9)):
        key = (t.kind, re.sub(r"\W+", " ", t.name.lower()).strip())
        if key not in merged:
            merged[key] = Target(t.name, kind=t.kind, number=t.number, chat=t.chat, source=t.source)
            order.append(key)
        else:
            m = merged[key]
            m.number = m.number or t.number
            m.chat = m.chat or t.chat
    return [merged[k] for k in order]


# ---------------------------------------------------------------- sending limits

@dataclass
class RateLimit:
    per_minute: int = 3
    per_hour: int = 20
    sent: list[float] = field(default_factory=list)

    def allowed(self) -> bool:
        now = time.time()
        self.sent = [t for t in self.sent if now - t < 3600]
        return (sum(1 for t in self.sent if now - t < 60) < self.per_minute and len(self.sent) < self.per_hour)

    def record(self):
        self.sent.append(time.time())


# ---------------------------------------------------------------- sending

_WA_ERRORS = {
    "locked": "Your Mac is locked, Sir. I can only send while it's unlocked.",
    "no-accessibility": ("I need Accessibility permission to press send in WhatsApp, Sir. I've opened the setting: "
                         "turn on Terminal, then ask me again."),
    "whatsapp-missing": "WhatsApp didn't open, Sir.",
    "text-not-in-box": ("WhatsApp didn't show the message as I expected, so I haven't sent anything, Sir. That chat "
                        "may already have a draft in it, or the number may not be on WhatsApp."),
    "not-confirmed": ("I pressed send but couldn't confirm it went, Sir. Please check WhatsApp before I try again."),
    "bad-number": "That number doesn't look right, Sir.",
    "bad-text": "That message is empty or too long, Sir.",
    "whatsapp-not-in-front": "WhatsApp didn't come to the front, so I stopped, Sir. Nothing was sent.",
    "no-search-field": "I couldn't find WhatsApp's search box, Sir, so I stopped. Nothing was sent.",
    "chat-not-found": "WhatsApp's search didn't show a chat with exactly that name, Sir. Nothing was sent.",
    "chat-not-verified": "I couldn't confirm the right chat had opened, so I didn't send it, Sir.",
    "no-message-box": "I couldn't find the message box in that chat, Sir. Nothing was sent.",
    "draft-present": "That chat already has a half-written message in it, Sir. I left it alone and sent nothing.",
}


def send_whatsapp_chat(chat: str, text: str, helper=None) -> None:
    """Send to a chat by its exact name (groups; people WhatsApp shows without a number)."""
    helper = helper or (lambda *a: _helper(*a, timeout=40))
    out = helper("wa-send-chat", chat, text)
    if isinstance(out, dict) and out.get("sent"):
        return
    code = out.get("error", "unknown") if isinstance(out, dict) else "unknown"
    if code == "no-accessibility":
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                       capture_output=True)
    raise MessagingError(code, _WA_ERRORS.get(code, "Something went wrong with WhatsApp, Sir. Nothing was sent."))


def send_whatsapp(number: str, text: str, country_code: str | None = None, helper=None) -> None:
    helper = helper or (lambda *a: _helper(*a, timeout=30))
    out = helper("wa-send", whatsapp_digits(number, country_code), text)
    if isinstance(out, dict) and out.get("sent"):
        return
    code = out.get("error", "unknown") if isinstance(out, dict) else "unknown"
    if code == "no-accessibility":
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
                       capture_output=True)
    raise MessagingError(code, _WA_ERRORS.get(code, "Something went wrong with WhatsApp, Sir. Nothing was sent."))


IMESSAGE_SCRIPT = '''on run argv
    set theNumber to item 1 of argv
    set theText to item 2 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        set buddy to participant theNumber of svc
        send theText to buddy
    end tell
end run'''


def send_imessage(number: str, text: str, country_code: str | None = None, runner=None) -> None:
    runner = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
    p = runner(["osascript", "-e", IMESSAGE_SCRIPT, e164(number, country_code), text])
    if p.returncode != 0:
        err = (p.stderr or "").lower()
        if "-1743" in err or "not allowed" in err:
            raise MessagingError("no-automation", "I'm not permitted to use Messages yet, Sir. Please allow Terminal "
                                 "to control Messages in System Settings, Privacy and Security, Automation.")
        raise MessagingError("imessage-failed", "Messages couldn't send that, Sir. The number may not use iMessage.")
