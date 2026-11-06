"""Downloads the free AI models Jarvis needs, and checks every byte.

    python setup/fetch_models.py            download what's missing (shows progress)
    python setup/fetch_models.py --json     the same, one JSON line per update (the setup window reads these)
    python setup/fetch_models.py --check    only check what's there; download nothing

Every file in setup/models.json has a fixed address (an exact version, not "latest"), a size and a
SHA-256 fingerprint. A download is written to a temporary file, checked, and only then moved into
place; anything that doesn't match is deleted. Interrupted downloads resume where they stopped.
Nothing is uploaded and no account or key is used: these are public files.

The brain models come through Ollama instead (see setup/setup.sh and the setup window).
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from jarvis_core.paths import MODELS  # noqa: E402

MANIFEST = json.loads((HERE / "models.json").read_text())
JSON = "--json" in sys.argv
CHUNK = 1 << 20


def say(**msg):
    if JSON:
        print(json.dumps(msg), flush=True)
    elif msg.get("event") == "progress":
        mb, total = msg["done"] / 1e6, msg["total"] / 1e6
        print(f"\r  {msg['file']}: {mb:,.0f} / {total:,.0f} MB", end="", flush=True)
    elif msg.get("event") in ("ok", "have", "error", "start", "done"):
        if msg.get("event") != "start":
            print()
        print({"ok": "  ✓ ", "have": "  ✓ already here: ", "error": "  ✗ ", "start": "→ ",
               "done": ""}[msg["event"]] + msg.get("text", ""), flush=True)


def context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def good(path: Path, item: dict) -> bool:
    return path.exists() and path.stat().st_size == item["size"] and sha256(path) == item["sha256"]


def fetch(item: dict, ctx: ssl.SSLContext, overall: dict) -> None:
    dest = MODELS / item["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if not item["url"].startswith("https://"):
        raise RuntimeError("refusing a non-HTTPS address")
    for attempt in range(4):
        have = part.stat().st_size if part.exists() else 0
        if have > item["size"]:
            part.unlink()
            have = 0
        req = urllib.request.Request(item["url"], headers={"User-Agent": "JarvisForMac-setup"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=60) as r:
                if have and r.status != 206:
                    have = 0                                   # the server ignored the resume: start over
                with open(part, "ab" if have else "wb") as f:
                    done, last = have, 0.0
                    while True:
                        block = r.read(CHUNK)
                        if not block:
                            break
                        f.write(block)
                        done += len(block)
                        overall["done"] += len(block)
                        if time.monotonic() - last > 0.25:
                            last = time.monotonic()
                            say(event="progress", file=item["path"], done=done, total=item["size"],
                                all_done=overall["done"], all_total=overall["total"])
            break
        except (OSError, urllib.error.URLError) as e:
            if attempt == 3:
                raise RuntimeError(f"download failed ({type(e).__name__})") from e
            time.sleep(2 * (attempt + 1))                      # a dropped connection: try again, resuming
    if part.stat().st_size != item["size"] or sha256(part) != item["sha256"]:
        part.unlink()
        raise RuntimeError("the file didn't match its fingerprint, so it was deleted")
    os.replace(part, dest)


def main() -> int:
    check_only = "--check" in sys.argv
    files = MANIFEST["files"]
    missing = []
    for item in files:
        if good(MODELS / item["path"], item):
            say(event="have", file=item["path"], text=item["path"])
        else:
            missing.append(item)
    if check_only:
        say(event="done", missing=len(missing), text=f"{len(missing)} model file(s) missing or damaged")
        return 1 if missing else 0
    overall = {"done": 0, "total": sum(i["size"] for i in missing)}
    for item in missing:
        part = MODELS / (item["path"] + ".part")
        overall["done"] += part.stat().st_size if part.exists() else 0
    ctx = context()
    for item in missing:
        say(event="start", file=item["path"], text=f"{item['what']}: {item['path']} ({item['size'] / 1e6:,.0f} MB)")
        try:
            fetch(item, ctx, overall)
        except Exception as e:
            say(event="error", file=item["path"], text=f"{item['path']}: {e}")
            return 1
        say(event="ok", file=item["path"], text=item["path"])
    say(event="done", missing=0, text="All models are here and checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
