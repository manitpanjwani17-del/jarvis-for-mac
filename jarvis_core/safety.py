"""Safety measures that apply to everything Jarvis does.

- Offline: Jarvis cannot open internet connections. Only connections to this Mac
  (localhost) are allowed. Any attempt to go further raises an error and is logged.
- One Jarvis at a time: a lock file stops two copies listening at once.
- Action log: every action is written to logs/actions.log, so you can see what
  Jarvis did and when. Audio is never saved.
- Confirmation words: only a clear spoken yes counts as yes (see is_yes).
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_log_path: Path | None = None
_log_commands = True


# ---------- action log ----------

def init_log(path: Path, log_commands: bool = True) -> None:
    global _log_path, _log_commands
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_path = path
    _log_commands = log_commands


def log(event: str, **details) -> None:
    """Append one line to the action log. Never raises."""
    if _log_path is None:
        return
    if not _log_commands:
        details.pop("heard", None)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **details}
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------- errors ----------

def log_error(where: str, exc: BaseException | None = None) -> None:
    """Write an error and its traceback to logs/errors.log (kept across restarts). Never raises."""
    import traceback
    if _log_path is None:
        return
    try:
        with open(_log_path.parent / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} in {where}\n")
            if exc is not None:
                f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            else:
                f.write(traceback.format_exc())
    except OSError:
        pass
    log("error", where=where, kind=type(exc).__name__ if exc else "unknown")


# ---------- offline guard ----------

def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


_WEB_PORTS = {80, 443}
_seen_hosts: set[str] = set()


def enforce_offline(allow_web: bool = False) -> None:
    """Refuse network connections except to this Mac itself and, when allow_web is on,
    ordinary web connections (ports 80 and 443) for the
    lookups you ask for. Every outside address contacted is written to the log once.

    Also tells the model libraries not to look for updates online.
    """
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def allowed(address) -> bool:
        if not isinstance(address, tuple):      # Unix sockets stay on this Mac
            return True
        host = _host_of(address)
        if host in _LOCAL_HOSTS:
            return True
        if allow_web and len(address) > 1 and address[1] in _WEB_PORTS:
            if host not in _seen_hosts:
                _seen_hosts.add(host)
                log("web_connection", host=host)
            return True
        return False

    def guarded_connect(self, address):
        if not allowed(address):
            log("blocked_network", host=_host_of(address))
            raise PermissionError("Jarvis may not open that network connection.")
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        if not allowed(address):
            log("blocked_network", host=_host_of(address))
            raise PermissionError("Jarvis may not open that network connection.")
        return original_connect_ex(self, address)

    def guarded_create(address, *args, **kwargs):
        if not allowed(address):
            log("blocked_network", host=_host_of(address))
            raise PermissionError("Jarvis may not open that network connection.")
        return original_create(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create


# ---------- single instance ----------

class SingleInstance:
    """Holds a lock file while Jarvis runs. A second copy refuses to start."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                os.kill(pid, 0)  # raises if that process is gone
                raise RuntimeError(f"Jarvis is already running (process {pid}).")
            except (ValueError, ProcessLookupError):
                pass  # stale lock from a crash
            except PermissionError:
                raise RuntimeError("Jarvis is already running.")
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


# ---------- confirmation ----------

def _norm(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z' ]+", " ", text)
    return " " + re.sub(r"\s+", " ", text).strip() + " "


def is_yes(text: str, yes_words, no_words) -> bool:
    """True only for a clear yes.

    Any no-word wins over a yes-word ("yes, actually no" is no). Silence, mumbling,
    or anything unrecognised is no. This is the gate for every action that leaves
    the Mac or cannot be undone.
    """
    t = _norm(text)
    if t.strip() == "":
        return False
    if any(f" {w} " in t for w in no_words):
        return False
    return any(f" {w} " in t for w in yes_words)


STOP_PHRASES = ("stop", "cancel", "never mind", "nevermind", "be quiet", "shut up", "that's all",
                "thats all", "nothing", "forget it")


def is_stop(text: str) -> bool:
    t = _norm(text).strip()
    return t in STOP_PHRASES or t in {f"jarvis {p}" for p in STOP_PHRASES}
