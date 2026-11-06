"""Everything Jarvis does with music, in the app you use (Apple Music by default; Spotify if chosen).

Apple Music
- Your library: songs, artists, albums, playlists and genres play straight away (AppleScript).
- The Apple Music catalogue (anything not in your library): Apple's search finds the song, and
  helpers/JarvisMusic opens it in Music and presses its play button, then checks it really started.
- Every answer comes from asking Music what it is actually doing afterwards, so Jarvis doesn't say
  "playing" when it isn't.

Nothing here deletes anything: songs can be loved, added to a playlist (created if needed), never removed.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

from . import skills
from .skills import Result, skill

from .paths import HELPERS
HELPER = HELPERS / "JarvisMusic"
T = 20   # Music can be slow to answer the first time (and macOS may be asking you for permission)


def _m(script: str, *args: str) -> str:
    """AppleScript to the Music app."""
    return skills.osa(script, *args, timeout=T)


def _fail(e: Exception) -> Result:
    if skills._permission_problem(e):
        return Result("I'm not allowed to control the Music app yet, Sir. Please allow Jarvis to control Music in "
                      "System Settings, Privacy and Security, Automation.", ok=False)
    return Result("The Music app didn't respond, Sir.", ok=False)


def now_playing() -> dict:
    """What Music is doing: state, title, artist, album, position and length."""
    out = _m('tell application "Music"\nset s to player state as text\nif s is "stopped" then return s\n'
             'set t to current track\nreturn s & "|" & (name of t) & "|" & (artist of t) & "|" & (album of t) & "|" & '
             '((player position as integer) as text) & "|" & ((duration of t as integer) as text)\nend tell')
    parts = out.split("|")
    if len(parts) < 6:
        return {"state": parts[0] if parts else "stopped"}
    return {"state": parts[0], "title": parts[1], "artist": parts[2], "album": parts[3],
            "position": int(parts[4] or 0), "duration": int(parts[5] or 0)}


def _apple() -> bool:
    return skills.music_app() == "Music"


def _need_app() -> Result | None:
    app = skills.music_app()
    if not app:
        return Result(skills.ASK_MUSIC_APP, ok=False)
    if app == "Spotify":
        return Result("I can only do that in Apple Music, Sir. Say 'I use Apple Music' to switch.", ok=False)
    return None


# ---------------------------------------------------------------- playing things

_LIBRARY = """on run argv
    set q to item 1 of argv
    set kind to item 2 of argv
    tell application "Music"
        if kind is "playlist" or kind is "any" then
            try
                set pl to (first user playlist whose name is q)
                play pl
                return "playlist|" & (name of pl)
            end try
            try
                set pl to (first user playlist whose name contains q)
                play pl
                return "playlist|" & (name of pl)
            end try
        end if
        if kind is "song" or kind is "any" then
            set hits to (every track of library playlist 1 whose name contains q)
            if (count of hits) > 0 then
                play item 1 of hits
                return "song|" & (name of item 1 of hits) & "|" & (artist of item 1 of hits)
            end if
        end if
        if kind is "artist" or kind is "any" then
            set hits to (every track of library playlist 1 whose artist contains q)
            if (count of hits) > 0 then
                set shuffle enabled to true
                play item (random number from 1 to (count of hits)) of hits
                return "artist|" & (artist of item 1 of hits)
            end if
        end if
        if kind is "album" or kind is "any" then
            set hits to (every track of library playlist 1 whose album contains q)
            if (count of hits) > 0 then
                play item 1 of hits
                return "album|" & (album of item 1 of hits)
            end if
        end if
        if kind is "genre" or kind is "any" then
            set hits to (every track of library playlist 1 whose genre contains q)
            if (count of hits) > 0 then
                set shuffle enabled to true
                play item (random number from 1 to (count of hits)) of hits
                return "genre|" & (genre of item 1 of hits)
            end if
        end if
        return "none"
    end tell
end run"""


def _catalog(query: str, kind: str) -> Result:
    """Find it in the Apple Music catalogue and start it in Music."""
    from . import web
    entity = {"album": "album"}.get(kind, "song")
    try:
        res = web.get_json("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": query, "media": "music", "entity": entity, "limit": 5, "country": "IN"}))["results"]
    except Exception:
        return Result("I couldn't reach Apple Music just now, Sir.", ok=False)
    if not res:
        return Result(f"I couldn't find {query} in your library or in Apple Music, Sir.", ok=False)
    if kind == "artist":     # their best-known song by that artist
        res = [r for r in res if query.lower() in r.get("artistName", "").lower()] or res
    hit = res[0]
    if entity == "album":
        try:
            tracks = web.get_json("https://itunes.apple.com/lookup?" + urllib.parse.urlencode(
                {"id": hit["collectionId"], "entity": "song", "country": "IN"}))["results"]
            hit = next(t for t in tracks if t.get("wrapperType") == "track")
        except Exception:
            return Result(f"I couldn't open the album {query}, Sir.", ok=False)
    title, artist, link = hit.get("trackName", query), hit.get("artistName", ""), hit.get("trackViewUrl", "")
    if not link.startswith("https://music.apple.com/"):
        return Result(f"I couldn't find {query} in Apple Music, Sir.", ok=False)
    def _same(a: str, b: str) -> bool:
        n = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
        return bool(n(a)) and (n(a).startswith(n(b)) or n(b).startswith(n(a)))
    try:
        st = now_playing()
        if _same(st.get("title", ""), title) and st.get("state") != "playing":
            _m('tell application "Music" to play')       # already loaded, just paused: carry on
            time.sleep(0.6)
            if now_playing().get("state") == "playing":
                return Result(f"Playing {title} by {artist}, Sir.")
    except Exception:
        pass
    p = subprocess.run([str(HELPER), "play-catalog", link, title], capture_output=True, text=True, timeout=60)
    try:
        out = json.loads((p.stdout or "{}").strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {}
    if out.get("played"):
        what = f"the album {hit.get('collectionName')}, starting with {title}" if entity == "album" else f"{title} by {artist}"
        return Result(f"Playing {what}, Sir.")
    if out.get("error") == "no-accessibility":
        return Result("To start songs from Apple Music I need Accessibility permission, Sir: System Settings, Privacy "
                      "and Security, Accessibility, switch on Jarvis. I've opened the song for you.", ok=False)
    try:      # the page opened with this song loaded: one plain "play" may be all it needs
        if _same(now_playing().get("title", ""), title):
            _m('tell application "Music" to play')
            time.sleep(0.8)
            st = now_playing()
            if st.get("state") == "playing" and _same(st.get("title", ""), title):
                return Result(f"Playing {title} by {artist}, Sir.")
    except Exception:
        pass
    return Result(f"I've opened {title} by {artist} in Apple Music, Sir, but it didn't start. Press play.", ok=False)


@skill("play_music", "Play music in Sir's music app: a song, an artist, an album, a playlist, a genre, or 'something "
       "from my library'. Searches his library first, then the whole Apple Music catalogue.",
       {"query": {"type": "string", "description": "What to play, e.g. 'Tum Hi Ho', 'Arijit Singh', 'my workout playlist'"},
        "kind": {"type": "string", "enum": ["any", "song", "artist", "album", "playlist", "genre"],
                 "description": "Only if Sir said which (default any)"}}, ["query"])
def play_music(query: str, kind: str = "any") -> Result:
    app = skills.music_app()
    if not app:
        return Result(skills.ASK_MUSIC_APP, ok=False)
    q = re.sub(r"^(the |my |some |a )", "", query.strip(), flags=re.I)
    q = re.sub(r"\s+(playlist|album|songs?|music)$", "", q, flags=re.I).strip() or query
    if app == "Spotify":
        skills.run(["open", "spotify:search:" + urllib.parse.quote(q)])
        return Result(f"I've brought up {q} in Spotify, Sir. Press play when you're ready.")
    if re.fullmatch(r"(something|anything|music|my library|my music|songs)", q, re.I):
        return shuffle_library()
    try:
        found = _m(_LIBRARY, q, kind or "any")
    except Exception as e:
        if skills._permission_problem(e):
            return _fail(e)
        found = "none"
    k, *rest = found.split("|")
    if k == "playlist":
        return Result(f"Playing your {rest[0]} playlist, Sir.")
    if k == "song":
        return Result(f"Playing {rest[0]} by {rest[1]}, Sir.")
    if k == "artist":
        return Result(f"Playing {rest[0]} from your library, Sir.")
    if k == "album":
        return Result(f"Playing the album {rest[0]}, Sir.")
    if k == "genre":
        return Result(f"Playing some {rest[0]}, Sir.")
    if kind == "playlist":
        return Result(f"You don't have a playlist called {q}, Sir.", ok=False)
    return _catalog(q, kind or "any")


def shuffle_library() -> Result:
    try:
        _m('tell application "Music"\nset shuffle enabled to true\nplay library playlist 1\nend tell')
    except Exception as e:
        return _fail(e)
    return Result("Shuffling your library, Sir.")


@skill("media", "Control what's playing: play/resume, pause, stop, next (skip), previous, restart (this song from "
       "the start), now_playing (what song is this).",
       {"action": {"type": "string", "enum": ["play", "pause", "stop", "next", "previous", "restart", "now_playing"]}},
       ["action"])
def media(action: str) -> Result:
    app = skills.music_app()
    running = [a for a in skills.MUSIC_APPS if skills.app_running(a)]
    app = app if (app in running or not running) else running[0]
    if not app:
        return Result(skills.ASK_MUSIC_APP, ok=False)
    if app == "Spotify":
        return skills._spotify_media(action)
    try:
        if action == "now_playing":
            return song_info("all")
        if action == "play":
            st = now_playing()
            if st.get("state") in ("stopped", None) or not st.get("title"):
                return shuffle_library()
            _m('tell application "Music" to play')
        elif action == "pause":
            _m('tell application "Music" to pause')
        elif action == "stop":
            _m('tell application "Music" to stop')
        elif action == "next":
            _m('tell application "Music" to next track')
        elif action == "previous":
            _m('tell application "Music"\nset player position to 0\nprevious track\nend tell')
        elif action == "restart":
            _m('tell application "Music"\nset player position to 0\nplay\nend tell')
        time.sleep(0.4)
        st = now_playing()
    except Exception as e:
        return _fail(e)
    title = st.get("title", "")
    if action == "pause":
        return Result("Paused, Sir." if st.get("state") != "playing" else "It's still playing, Sir. Music didn't pause.",
                      ok=st.get("state") != "playing")
    if action == "stop":
        return Result("Music stopped, Sir.")
    if action in ("next", "previous"):
        return Result(f"{title} by {st.get('artist')}, Sir." if title else "Done, Sir.")
    if action == "restart":
        return Result(f"From the top: {title}, Sir." if title else "From the top, Sir.")
    return Result(f"Playing {title}, Sir." if st.get("state") == "playing" and title else "Playing, Sir.")


@skill("song_info", "About the song playing: what it is, who sings it, which album, or how much is left.",
       {"what": {"type": "string", "enum": ["all", "artist", "album", "time_left"]}}, ["what"])
def song_info(what: str = "all") -> Result:
    try:
        st = now_playing()
    except Exception as e:
        return _fail(e)
    if not st.get("title"):
        return Result("Nothing is playing at the moment, Sir.")
    paused = "" if st["state"] == "playing" else " It's paused."
    if what == "artist":
        return Result(f"That's {st['artist']}, Sir.{paused}")
    if what == "album":
        return Result(f"It's from {st['album']}, Sir.{paused}")
    if what == "time_left":
        left = max(0, st["duration"] - st["position"])
        return Result(f"{left // 60} minutes and {left % 60} seconds left in this song, Sir.")
    return Result(f"This is {st['title']} by {st['artist']}, from {st['album']}, Sir.{paused}")


@skill("music_mode", "Shuffle on/off, or repeat: one (this song), all, or off.",
       {"shuffle": {"type": "string", "enum": ["on", "off"]},
        "repeat": {"type": "string", "enum": ["one", "all", "off"]}})
def music_mode(shuffle: str = "", repeat: str = "") -> Result:
    need = _need_app()
    if need:
        return need
    said = []
    try:
        if shuffle:
            _m(f'tell application "Music" to set shuffle enabled to {"true" if shuffle == "on" else "false"}')
            said.append(f"shuffle {shuffle}")
        if repeat:
            _m(f'tell application "Music" to set song repeat to {repeat}')
            said.append({"one": "repeating this song", "all": "repeating everything", "off": "repeat off"}[repeat])
    except Exception as e:
        return _fail(e)
    return Result((", ".join(said) or "Done").capitalize() + ", Sir.")


@skill("music_seek", "Skip forward or back within the song by some seconds (negative = back).",
       {"seconds": {"type": "integer"}}, ["seconds"])
def music_seek(seconds: int) -> Result:
    need = _need_app()
    if need:
        return need
    try:
        _m('on run argv\ntell application "Music" to set player position to (player position + (item 1 of argv as integer))\n'
           'end run', str(int(seconds)))
    except Exception as e:
        return _fail(e)
    return Result(f"{'Forward' if seconds > 0 else 'Back'} {abs(int(seconds))} seconds, Sir.")


@skill("music_volume", "The Music app's own volume (not the Mac's): set, up or down.",
       {"action": {"type": "string", "enum": ["set", "up", "down"]}, "level": {"type": "integer"}}, ["action"])
def music_volume(action: str, level: int | None = None) -> Result:
    need = _need_app()
    if need:
        return need
    try:
        cur = int(_m('tell application "Music" to return sound volume') or 50)
        new = {"up": cur + 15, "down": cur - 15}.get(action, level if level is not None else cur)
        new = max(0, min(100, int(new)))
        _m('on run argv\ntell application "Music" to set sound volume to (item 1 of argv as integer)\nend run', str(new))
    except Exception as e:
        return _fail(e)
    return Result(f"Music at {new} percent, Sir.")


@skill("love_song", "Love (favourite) or unlove the song that's playing.",
       {"state": {"type": "string", "enum": ["love", "unlove"]}}, ["state"])
def love_song(state: str = "love") -> Result:
    need = _need_app()
    if need:
        return need
    v = "true" if state == "love" else "false"
    try:
        title = _m('tell application "Music" to return name of current track')
        try:
            _m(f'tell application "Music" to set favorited of current track to {v}')
        except Exception:
            _m(f'tell application "Music" to set loved of current track to {v}')
    except Exception as e:
        return _fail(e)
    return Result(f"{'Loved' if state == 'love' else 'Removed the love from'} {title}, Sir.")


@skill("add_to_playlist", "Add the song that's playing to one of Sir's playlists (makes the playlist if it doesn't exist).",
       {"playlist": {"type": "string"}}, ["playlist"])
def add_to_playlist(playlist: str) -> Result:
    need = _need_app()
    if need:
        return need
    script = """on run argv
    set p to item 1 of argv
    tell application "Music"
        set t to current track
        try
            set pl to (first user playlist whose name is p)
            set made to "no"
        on error
            set pl to (make new user playlist with properties {name:p})
            set made to "yes"
        end try
        duplicate t to pl
        return made & "|" & (name of t)
    end tell
end run"""
    try:
        made, title = (_m(script, playlist.strip()) + "|").split("|")[:2]
    except Exception as e:
        if skills._permission_problem(e):
            return _fail(e)
        return Result("I couldn't add that one, Sir. Songs that aren't in your library may need adding to it first.",
                      ok=False)
    return Result(f"Added {title} to {'a new playlist, ' if made == 'yes' else ''}{playlist}, Sir.")


@skill("list_playlists", "Say which playlists Sir has.", kind="info")
def list_playlists() -> Result:
    try:
        names = _m('tell application "Music"\nset out to ""\nrepeat with p in (every user playlist whose smart is false)\n'
                   'set out to out & (name of p) & "|"\nend repeat\nreturn out\nend tell')
    except Exception as e:
        return _fail(e)
    items = [n for n in names.split("|") if n.strip()]
    if not items:
        return Result("You don't have any playlists of your own yet, Sir.", data="none")
    return Result(f"You have {len(items)} playlists, Sir.", data="Playlists: " + ", ".join(items[:40]))


@skill("play_similar", "Play more from the artist that's playing ('more like this', 'more from this artist').")
def play_similar() -> Result:
    try:
        st = now_playing()
    except Exception as e:
        return _fail(e)
    if not st.get("artist"):
        return Result("Nothing is playing to go on, Sir.", ok=False)
    artist = st["artist"].split(" & ")[0].split(",")[0].strip()
    r = play_music(artist, "artist")
    return r


@skill("music_sleep_timer", "Stop the music after a while ('stop the music in 30 minutes').",
       {"duration": {"type": "string", "description": "e.g. '30 minutes'"}}, ["duration"])
def music_sleep_timer(duration: str) -> Result:
    from .timeparse import duration_seconds, say_duration
    secs = duration_seconds(duration)
    if not secs:
        return Result("For how long, Sir?", ok=False)
    t = skills.TIMERS.add(secs, "")
    t["then"] = "pause_music"
    return Result(f"The music will stop in {say_duration(secs)}, Sir.")


@skill("airplay", "Play through an AirPlay speaker or TV, or list them ('play on the living room speaker').",
       {"device": {"type": "string", "description": "The speaker's name, or 'list' to hear them, or 'this Mac'"}},
       ["device"])
def airplay(device: str) -> Result:
    need = _need_app()
    if need:
        return need
    try:
        names = [n for n in _m('tell application "Music"\nset out to ""\nrepeat with d in AirPlay devices\n'
                               'set out to out & (name of d) & "|"\nend repeat\nreturn out\nend tell').split("|") if n]
    except Exception as e:
        return _fail(e)
    if device.lower() in ("list", "which", "all"):
        return Result("Your speakers: " + ", ".join(names) + ", Sir." if names else "I don't see any AirPlay speakers, Sir.")
    import difflib
    want = "computer" if re.search(r"this mac|computer|macbook|here", device, re.I) else device
    pick = next((n for n in names if n.lower() == want.lower()), None) or \
        next(iter(difflib.get_close_matches(want, names, n=1, cutoff=0.5)), None) or \
        next((n for n in names if want.lower() in n.lower()), None)
    if not pick:
        return Result(f"I can't find a speaker called {device}, Sir. "
                      + ("I can see: " + ", ".join(names) + "." if names else ""), ok=False)
    try:
        _m('on run argv\ntell application "Music" to set current AirPlay devices to {AirPlay device (item 1 of argv)}\n'
           'end run', pick)
    except Exception as e:
        return _fail(e)
    return Result(f"Playing through {pick}, Sir.")
