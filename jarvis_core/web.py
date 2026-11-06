"""Live answers from the web: weather, news, Wikipedia, and general web lookups.

Read-only. Jarvis only fetches pages; he never fills in forms, signs in, posts, buys or
uploads anything. The only thing sent is what's needed for the lookup (the search words
or the city). Everything fetched is treated as information, never as instructions.

Uses only Python's standard library; no new packages.
"""
from __future__ import annotations

import html
import json
import re
import urllib.parse
import ssl
import urllib.request
from html.parser import HTMLParser

import certifi   # trusted certificate list (already installed with the speech packages)

_TLS = ssl.create_default_context(cafile=certifi.where())

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
TIMEOUT = 8


class WebError(Exception):
    pass


def get(url: str, timeout: float = TIMEOUT, limit: int = 1_500_000) -> str:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise WebError("only web pages")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as r:
            raw = r.read(limit)
            charset = r.headers.get_content_charset() or "utf-8"
    except Exception as e:
        raise WebError(type(e).__name__)
    return raw.decode(charset, errors="replace")


def get_json(url: str) -> dict:
    try:
        return json.loads(get(url))
    except ValueError:
        raise WebError("bad reply")


# ---------------------------------------------------------------- page text

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button", "iframe"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.depth:
            self.parts.append(data)


def page_text(url: str, limit: int = 3000) -> tuple[str, str]:
    """(title, readable text) of a page, trimmed."""
    p = _Text()
    p.feed(get(url))
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 40]   # drop menus and fragments
    return p.title.strip(), "\n".join(lines)[:limit]


# ---------------------------------------------------------------- search

def search(query: str, n: int = 5) -> list[dict]:
    """Web results: [{"title", "url", "snippet"}]."""
    body = get("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query, "kl": "in-en"}))
    results = []
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="result__a"|$)',
                         body, re.S):
        href, title, rest = m.group(1), m.group(2), m.group(3)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(href)).query)
        url = q.get("uddg", [html.unescape(href)])[0]
        if "duckduckgo.com/y.js" in url or not url.startswith("http"):
            continue   # adverts
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', rest, re.S)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
        results.append({"title": clean(title), "url": url, "snippet": clean(sm.group(1) if sm else "")})
        if len(results) >= n:
            break
    return results


def look_up(query: str) -> str:
    """Search, then read the top pages, and return facts for Jarvis to answer from."""
    results = search(query)
    if not results:
        raise WebError("no results")
    parts = [f"Search results for: {query}"]
    for r in results[:5]:
        parts.append(f"- {r['title']}: {r['snippet']} ({urllib.parse.urlparse(r['url']).hostname})")
    from concurrent.futures import ThreadPoolExecutor, wait

    def read(r):
        title, text = page_text(r["url"], limit=1800)
        return r, title, text

    pool = ThreadPoolExecutor(max_workers=3)
    futures = [pool.submit(read, r) for r in results[:3]]
    done, _ = wait(futures, timeout=4.5)          # whatever arrives in time
    pool.shutdown(wait=False, cancel_futures=True)
    for f in futures:
        if f in done and not f.exception():
            r, title, text = f.result()
            if text:
                parts.append(f"\nFrom {urllib.parse.urlparse(r['url']).hostname} ({title[:80]}):\n{text}")
    return "\n".join(parts)[:5000]


# ---------------------------------------------------------------- Wikipedia

def wikipedia(topic: str) -> str:
    found = get_json("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "opensearch", "search": topic, "limit": 1, "namespace": 0, "format": "json"}))
    if not found or len(found) < 2 or not found[1]:
        raise WebError("not found")
    title = found[1][0]
    s = get_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")))
    return f"Wikipedia, {s.get('title', title)}: {s.get('extract', '')}"[:2500]


# ---------------------------------------------------------------- weather

_WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
        51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
        66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
        80: "light showers", 81: "showers", 82: "violent showers", 95: "thunderstorms",
        96: "thunderstorms with hail", 99: "severe thunderstorms with hail"}


def weather(place: str) -> str:
    g = get_json("https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": place, "count": 1, "language": "en", "format": "json"}))
    if not g.get("results"):
        raise WebError("place not found")
    loc = g["results"][0]
    f = get_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"}))
    c, d = f.get("current", {}), f.get("daily", {})
    where = ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x)
    lines = [f"Weather for {where}. Now: {_WMO.get(c.get('weather_code'), 'unknown conditions')}, "
             f"{c.get('temperature_2m')} degrees Celsius (feels like {c.get('apparent_temperature')}), humidity "
             f"{c.get('relative_humidity_2m')} percent, wind {c.get('wind_speed_10m')} km/h."]
    for i, day in enumerate(("Today", "Tomorrow", "Day after tomorrow")):
        try:
            lines.append(f"{day}: {_WMO.get(d['weather_code'][i], 'mixed')}, high {d['temperature_2m_max'][i]}, "
                         f"low {d['temperature_2m_min'][i]}, chance of rain {d['precipitation_probability_max'][i]} percent, "
                         f"sunrise {d['sunrise'][i][-5:]}, sunset {d['sunset'][i][-5:]}.")
        except (KeyError, IndexError, TypeError):
            break
    return " ".join(lines)


# ---------------------------------------------------------------- news

def news(topic: str = "", n: int = 6) -> str:
    base = "https://news.google.com/rss"
    params = {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    url = (f"{base}/search?" + urllib.parse.urlencode({"q": topic, **params})) if topic else f"{base}?" + urllib.parse.urlencode(params)
    xml = get(url)
    items = re.findall(r"<item>.*?<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>", xml, re.S)
    if not items:
        raise WebError("no news")
    heads = [html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t)).strip() for t, _ in items[:n]]
    return f"Latest headlines{' about ' + topic if topic else ''}: " + " | ".join(heads)
