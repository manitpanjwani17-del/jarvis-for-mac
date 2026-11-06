"""Small preferences you change by voice, kept between runs (data/settings.json).

For example: reading new messages aloud on or off, which chats, his speaking speed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import DATA

FILE = DATA / "settings.json"

DEFAULTS = {
    "address": "Sir",               # how Jarvis addresses you: "Sir", "Ma'am", or your name
    "country_code": "",             # added to phone numbers said without one ("my country code is 44")
    "intro_done": False,            # the one-time introduction (memory, voice training) has been given
    "read_new_messages": False,     # never read new messages aloud unless you turn it on
    "read_new_from": [],            # empty = every chat; otherwise only these chats
    "voice_speed": None,            # None = the speed in config.py
    "music_app": None,              # "Music" (Apple Music) or "Spotify"; asked once if both are installed
    "only_my_voice": True,          # once your voice is trained, ignore other people
    "voice_training_offered": False,
    "hearing_model": None,          # the speech model that understood you best at training
    "dictation": True,              # hold fn to dictate into any text box
    "dictation_polish": "full",     # full (rules + local model), light (rules only), off (exactly as heard)
    "dictation_setup_told": False,  # told you once how to set the fn key up
}


def load() -> dict:
    try:
        data = json.loads(FILE.read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}


def save(**changes) -> dict:
    data = load()
    data.update({k: v for k, v in changes.items() if k in DEFAULTS})
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return data
