"""Start Jarvis.

    ./jarvis              listen for "Hey Jarvis" and answer
    ./jarvis say "text"   hear Jarvis say something
    ./jarvis log          show what Jarvis has done
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_core import safety  # noqa: E402
from jarvis_core.config import Config  # noqa: E402


def audio_test(cfg) -> dict:
    """Plays one sentence and measures how much of it the echo-cancelled microphone still hears.
    Nothing is recorded or kept: only loudness numbers and the speech detector's verdicts."""
    import time as _t
    import numpy as np
    from jarvis_core.audio_io import DuplexAudio
    from jarvis_core.ears import SpeechDetector
    from jarvis_core.voice import Voice
    import subprocess as _sp
    probe = _sp.run([str(Path(__file__).resolve().parent / "helpers" / "JarvisAudio"), "--probe"],
                    capture_output=True, text=True, timeout=30).stderr.strip().splitlines()
    probe_result = json.loads(probe[-1]) if probe else {}
    mic = DuplexAudio(cfg.frame_samples)
    if not mic.start():
        return {"started": False, "why": mic.failed, "events": mic.events, **probe_result}
    det = SpeechDetector(cfg)
    voice = Voice(cfg.voice)
    voice.output = mic
    rms = lambda f: float(np.sqrt(np.mean((f.astype(np.float32) / 32768) ** 2)))
    _t.sleep(0.6); mic.flush()
    quiet = [rms(f) for f in (mic.read(1.0) for _ in range(12)) if f is not None]
    audio = voice.render("Good evening, Sir. This is a short test of my hearing while I speak, so please stay quiet for a moment.")
    import threading
    th = threading.Thread(target=voice.play, args=(audio,), daemon=True)
    mic.flush(); th.start()
    during, run, longest = [], 0, 0
    while th.is_alive():
        f = mic.read(0.2)
        if f is None:
            continue
        during.append(rms(f))
        run = run + 1 if det.prob(f) >= cfg.listen.barge_threshold else 0
        longest = max(longest, run)
    mic.stop()
    q = float(np.median(quiet)) if quiet else 0.0
    d = float(np.median(during)) if during else 0.0
    return {"started": True, "echo_cancellation": mic.aec, "setup": mic.setup, "notes": mic.events, "room_level": round(q, 5), "level_while_speaking": round(d, 5),
            "loudest_while_speaking": round(max(during) if during else 0.0, 5),
            "longest_voice_run_frames": longest, "would_falsely_interrupt": longest >= cfg.listen.barge_frames}


def dictate_test(ask: str) -> dict:
    """Starts the fn helper and reports what it can do. `ask` may be "paste:<text>" or "card:<text>" (test text
    only), to type it into the focused box or show the card. Reports no text from the screen, ever."""
    import time as _t
    from jarvis_core.dictate import Helper
    h = Helper()
    if not h.start():
        return {"started": False}
    end = _t.monotonic() + 3
    while _t.monotonic() < end and not (h.ready or h.error):
        _t.sleep(0.1)
    out = {"started": True, "ready": h.ready, "error": h.error,
           "status": h.request("status", cmd="status"), "target": h.request("target", cmd="target")}
    if ask.startswith("paste:") and (out["target"] or {}).get("editable"):
        out["pasted"] = h.request("pasted", cmd="paste", text=ask[6:])
        _t.sleep(1.0)              # let the clipboard be put back
    elif ask.startswith("card:"):
        out["card"] = h.request("card", cmd="card", text=ask[5:].replace("\\n", "\n"))
        _t.sleep(4)
    if ask.startswith("pill"):
        for st in ("listening", "working", "done"):
            h.pill(st, "Typed" if st == "done" else "")
            _t.sleep(1.2)
    h.close()
    return out


def main(argv: list[str]) -> int:
    cfg = Config()
    safety.init_log(cfg.safety.log_file, cfg.safety.log_commands)
    if cfg.safety.offline:
        safety.enforce_offline(allow_web=cfg.safety.allow_web)
    cmd = argv[0] if argv else "run"

    if cmd == "say":
        from jarvis_core.voice import Voice
        Voice(cfg.voice).say(" ".join(argv[1:]) or "At your service, Sir.", use_cache=False)
        return 0

    if cmd == "samples":
        from tests.voice_samples import make_samples
        make_samples(cfg)
        return 0

    if cmd == "check":
        # Everything Jarvis needs, without listening, looking or reading anything private.
        import json as _json
        import urllib.request as _u
        from jarvis_core.brain import Brain
        from jarvis_core.paths import HELPERS, LOGS
        out = {"models": all(p.exists() for p in [cfg.wake.model, cfg.voice.model, cfg.voice.voices,
                                                 cfg.hearing.model_dir / "model.bin", cfg.hearing.wake_check_dir / "model.bin"]),
               "helpers": {h: (HELPERS / h).exists() for h in ("JarvisAudio", "JarvisPIM", "JarvisMessenger", "JarvisScreen", "JarvisDictate", "JarvisMusic")},
               "hud": cfg.hud.binary.exists()}
        b = Brain(cfg.brain)
        out["ollama"] = b.ensure_running(wait_s=15)
        if out["ollama"]:
            tags = _json.loads(_u.urlopen(cfg.brain.url + "/api/tags", timeout=5).read())
            names = {m["name"] for m in tags.get("models", [])}
            out["brain_model"] = cfg.brain.model in names
            out["vision_model"] = cfg.brain.vision_model in names
        out["started_by_app"] = os.environ.get("JARVIS_APP") == "1"
        mflag = LOGS / "musictest.request"
        if mflag.exists():          # a one-off check that Jarvis may control the Music app
            mflag.unlink()
            import subprocess as _sp
            p = _sp.run(["osascript", "-e", 'tell application "Music" to get player state'],
                        capture_output=True, text=True, timeout=60)
            out["musictest"] = {"code": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()[:300]}
        dflag = LOGS / "dictatetest.request"
        if dflag.exists():          # fn dictation helper: its permissions, and where it would type
            ask = dflag.read_text().strip()
            dflag.unlink()
            out["dictatetest"] = dictate_test(ask)
        flag = LOGS / "audiotest.request"
        if flag.exists():           # a one-off echo test, asked for by creating this file
            flag.unlink()
            out["audiotest"] = audio_test(cfg)
        print(_json.dumps(out))
        return 0 if out["models"] and all(out["helpers"].values()) and out.get("brain_model") else 1

    if cmd == "log":
        p = cfg.safety.log_file
        print(p.read_text() if p.exists() else "Nothing logged yet.")
        return 0

    if cmd != "run":
        print(__doc__)
        return 1

    from jarvis_core.assistant import Assistant
    from jarvis_core.ears import Microphone

    try:
        with safety.SingleInstance(cfg.safety.lock_file):
            print("Starting Jarvis… (loading models)")
            from jarvis_core.audio_io import DuplexAudio
            mic = DuplexAudio(cfg.frame_samples)
            if mic.start():
                print("Echo cancellation on: you can talk over me." if mic.aec else
                      "Echo cancellation unavailable: interrupt me with “Hey Jarvis”.")
            else:
                print(f"(Echo-cancelling audio didn't start: {mic.failed}. Using the plain microphone.)")
                mic = Microphone(cfg)
            jarvis = Assistant(cfg, mic)
            if isinstance(mic, DuplexAudio):
                jarvis.voice.output = mic
            def _term(*a):
                safety.log("stop_requested", by="the app (Quit or Restart)")
                jarvis.running = False
            signal.signal(signal.SIGTERM, _term)
            if not isinstance(mic, DuplexAudio):
                mic.start()
            safety.log("started", echo_cancellation=bool(getattr(mic, "aec", False)))
            import threading as _th
            _th.excepthook = lambda a: safety.log_error(f"thread {a.thread.name if a.thread else ''}", a.exc_value)
            reason = "unknown"
            crashes: list[float] = []
            greet = True
            try:
                while True:
                    try:
                        jarvis.run(greet=greet)
                        reason = "asked to stop" if not jarvis.running else "ended"
                        break
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        # Something unexpected: note it, and carry on listening rather than closing.
                        safety.log_error("main loop", e)
                        import time as _time
                        crashes = [t for t in crashes if _time.monotonic() - t < 300] + [_time.monotonic()]
                        if len(crashes) > 5:
                            reason = "too many errors"
                            raise
                        greet = False
                        jarvis.running = True
            except KeyboardInterrupt:
                reason = "Ctrl+C"
                print("\nStopping.")
            finally:
                mic.stop()
                jarvis.close()
                safety.log("stopped", reason=reason)
    except RuntimeError as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
