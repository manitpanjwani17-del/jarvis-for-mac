"""Where Jarvis keeps things.

The code can sit anywhere (a cloned folder, or inside Jarvis.app). Everything Jarvis makes or
downloads lives in one folder of the user's own, so deleting that folder removes it all:

    ~/Library/Application Support/Jarvis for Mac/
        data/     your settings, memory, people, nicknames, voiceprint (never leaves the Mac)
        logs/     what Jarvis did (who was messaged, never the words)
        models/   the free AI models downloaded at setup
        cache/    pre-made voice phrases
        venv/     Jarvis's own Python

JARVIS_HOME overrides that folder (tests use a temporary one). JARVIS_HELPERS points at prebuilt
helper programs (inside Jarvis.app); otherwise they're built next to the code.
"""
from __future__ import annotations

import os
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("JARVIS_HOME") or Path.home() / "Library" / "Application Support" / "Jarvis for Mac")
DATA = HOME / "data"
LOGS = HOME / "logs"
MODELS = HOME / "models"
CACHE = HOME / "cache"
HELPERS = Path(os.environ.get("JARVIS_HELPERS") or CODE / "helpers")
OVERLAY = Path(os.environ.get("JARVIS_OVERLAY") or CODE / "overlay")
ASSETS = CODE / "assets"
# Inside Jarvis.app the helpers are already built and signed; never try to compile there.
PACKAGED = bool(os.environ.get("JARVIS_HELPERS"))
