"""Download sources, ranked best to worst, with candidate search and
censored/altered-version filtering.

The pipeline's goal is the original UNCENSORED (explicit) studio master.
Uploads that are clean-edited, altered, or not the original recording are
rejected before download, and re-checked by the LLM gate after download.
"""
import difflib
import html
import json
import os
import re
import time
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from pathlib import Path

YTDLP = os.environ.get("TUNESTER_YTDLP", "yt-dlp")
SEARCH_DEPTH = int(os.environ.get("TUNESTER_SEARCH_DEPTH", "15"))
SEARCH_TIMEOUT = int(os.environ.get("TUNESTER_SEARCH_TIMEOUT", "120"))
HTTP_TIMEOUT = int(os.environ.get("TUNESTER_HTTP_TIMEOUT", "60"))

# nzb.life (Usenet) — highest-priority source. Index-only Newznab: search +
# .nzb manifest; the bytes come over NNTP, so NNTP provider credentials are
# required too. Its WAF 403s non-browser user agents.
NZB_API = os.environ.get("TUNESTER_NZB_API", "https://api.nzb.life")
NZB_APIKEY = os.environ.get("TUNESTER_NZB_APIKEY", "")
NZB_I = os.environ.get("TUNESTER_NZB_I", "")
NNTP_HOST = os.environ.get("TUNESTER_NNTP_HOST", "")
NNTP_PORT = int(os.environ.get("TUNESTER_NNTP_PORT", "563"))
NNTP_USER = os.environ.get("TUNESTER_NNTP_USER", "")
NNTP_PASS = os.environ.get("TUNESTER_NNTP_PASS", "")
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

# Words in a candidate title that mark a censored or altered recording.
# The ytcookies exporter publishes the browser's live user agent next to the
# shared cookies (same file DrivePod's harvest gate reads). The browser that
# created the session is the UA source of truth; a mismatched UA on the same
# session is a kill signal, so yt-dlp runs carry it too.
SESSION_STATE_FILE = Path("/shared-cookies/session-state.json")
SESSION_STATE_MAX_AGE = 30 * 60  # the exporter rewrites it every ~10.5 min


def browser_user_agent() -> str | None:
    """The shared session's browser UA, if the state file is fresh."""
    try:
        data = json.loads(SESSION_STATE_FILE.read_text())
        if time.time() - float(data.get("ts", 0)) > SESSION_STATE_MAX_AGE:
            return None
        return data.get("ua") or None
    except (OSError, ValueError):
        return None


NEVER_WORDS = {
    "live", "cover", "remix", "remixes", "remixed", "covers", "slowed",
    "nightcore", "karen", "karaoke", "instrumental", "instrumentals",
    "capella", "lyric", "lyrics", "tutorial", "reaction", "compilation",
    "mashup", "mashups", "parody", "parodies", "tribute", "tributes",
    "snippet", "snippets", "preview", "previews", "trailer", "demo",
    "demos", "unofficial", "reverb", "reverbs", "key", "ukulele",
}
NEVER_PHRASES = (
    "clean version", "clean edit", "radio edit", "radio version",
    "slowed reverb", "speed up", "sped up", "lyric video", "lyrics video",
    "off key", "piano cover", "a capella", "explicit removed",
)
NEVER_PARENS = re.compile(r"(?i)\((clean|radio\s*edit|censored)\)")


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    kind: str  # ytsearch | scsearch | archive | ytsearch_loose


# Best to worst: coverage x integrity x reliability.
# Backburner (2026-09-26): YouTube is a poor source for uncensored studio
# originals, and this VPS IP gets bot-checked (the player response also
# needs the yt-dlp EJS JS-runtime signature solver; see yt-dlp/yt-dlp/wiki/EJS).
# To re-enable, restore these two entries and ship a JS runtime in the image:
#     Source("youtube", "YouTube", "ytsearch"),
#     Source("youtube-loose", "YouTube (deep)", "ytsearch_loose"),
SOURCES = [
    Source("nzb", "nzb.life (Usenet)", "nzb"),
    Source("soundcloud", "SoundCloud", "scsearch"),
    Source("archive", "archive.org", "archive"),
]


@dataclass
class Candidate:
    source: str
    url: str
    title: str
    uploader: str = ""
    duration: float | None = None
    popularity: int = 0
    score: int = 0
    posted: float = 0.0  # release post timestamp (nzb.pubDate); 0 = unknown


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def make_bad_filter(artist: str):
    """Return a predicate rejecting censored/altered candidate titles.

    Words that are also part of the artist name never trigger rejection
    (e.g. the band "Live", or "Clean Bandit").
    """
    artist_words = set(_words(artist))

    def bad(title: str) -> str | None:
        t = title.lower()
        for phrase in NEVER_PHRASES:
            if phrase in t:
                return phrase
        if NEVER_PARENS.search(title):
            return "(censored)"
        for w in _words(title):
            if w in NEVER_WORDS and w not in artist_words:
                return w
        return None

    return bad


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def title_present(cand_title: str, target_title: str) -> bool:
    """True if the candidate title plausibly contains the target song title."""
    a, b = _norm(cand_title), _norm(target_title)
    if b and b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _score(cand: Candidate, artist: str) -> None:
    """Prefer explicit-tagged, artist-attributed, official uploads."""
    t = cand.title.lower()
    a = artist.lower()
    s = 0
    if "(explicit)" in t:
        s += 4
    if a and a in t:
        s += 2
    if cand.uploader and a and a in cand.uploader.lower():
        s += 3
    if "official" in t:
        s += 1
    cand.score = s


def _yt_search(kind: str, artist: str, title: str, cookies: Path | None) -> list[Candidate]:
    depth = SEARCH_DEPTH * (3 if kind == "ytsearch_loose" else 1)
    prefix = "ytsearch" if kind.startswith("yt") else "scsearch"
    cmd = [YTDLP, f"{prefix}{depth}:{artist} {title}",
           "--flat-playlist", "--dump-json", "--no-warnings"]
    if kind.startswith("yt") and cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
        ua = browser_user_agent()
        if ua:
            cmd += ["--user-agent", ua]
    proc = subprocess.run(cmd, capture_output=True, timeout=SEARCH_TIMEOUT)
    cands: list[Candidate] = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = e.get("id") or ""
        if kind.startswith("yt"):
            url = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
            popularity = e.get("view_count") or 0
            uploader = e.get("channel") or e.get("uploader") or ""
        else:
            url = e.get("url") or ""
            popularity = e.get("playback_count") or 0
            uploader = e.get("uploader") or e.get("channel") or ""
        if not url:
            continue
        cands.append(Candidate(
            source=kind,
            url=url,
            title=e.get("title") or vid,
            uploader=uploader,
            duration=e.get("duration"),
            popularity=popularity,
        ))
    return cands


def _parse_length(value) -> float | None:
    """archive.org file 'length': seconds, 'M:SS', or 'H:MM:SS'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        secs = 0.0
        for part in s.split(":"):
            secs = secs * 60 + float(part)
        return secs if secs > 0 else None
    except ValueError:
        return None


def _archive_search(artist: str, title: str, album: str = "") -> list[Candidate]:
    """Title query first; full-album uploads rarely mention individual
    track names in item metadata, so fall back to the album name and
    let the per-file title filter pluck the track."""
    queries = [f"{artist} {title} AND mediatype:(audio)"]
    if album:
        queries.append(f"{artist} {album} AND mediatype:(audio)")
    cands: list[Candidate] = []
    bad = make_bad_filter(artist)
    want = _norm(title)
    for qi, q in enumerate(queries):
        url = ("https://archive.org/advancedsearch.php?"
               f"q={urllib.parse.quote(q)}&fl%5B%5D=identifier&rows=25&output=json")
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:
                data = json.load(resp)
        except Exception:
            continue
        docs = (data.get("response") or {}).get("docs") or []
        if not docs:
            continue
        if qi > 0 and cands:
            break  # only fall back to the album query when nothing found
        for d in docs[:15]:
            ident = d.get("identifier")
            if not ident:
                continue
            try:
                with urllib.request.urlopen(
                        f"https://archive.org/metadata/{ident}",
                        timeout=HTTP_TIMEOUT) as resp:
                    meta = json.load(resp)
            except Exception:
                continue
            md = meta.get("metadata") or {}
            uploader = str(md.get("creator") or md.get("creator__sort") or "")
            for f in meta.get("files") or []:
                name = f.get("name") or ""
                if "." not in name:
                    continue
                ext = name.rsplit(".", 1)[-1].lower()
                if ext not in ("mp3", "m4a", "ogg", "flac", "opus", "wav"):
                    continue
                base = name.rsplit(".", 1)[0]
                if want and want not in _norm(base):
                    continue
                if bad(base):
                    continue
                cands.append(Candidate(
                    source="archive",
                    url=f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}",
                    title=base,
                    uploader=uploader,
                    duration=_parse_length(f.get("length")),
                ))
    return cands


def nzb_configured() -> bool:
    return bool(NZB_APIKEY and NNTP_HOST and NNTP_USER)


def _nzb_get(url: str, timeout: int, tries: int = 4) -> bytes:
    """GET nzb.life with backoff (429/5xx burst); a 24h per-release
    'Already Downloaded' cooldown 429 is raised, not retried."""
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and "already downloaded" in \
                    e.read().decode(errors="replace").lower():
                raise RuntimeError(
                    "nzb.life getnzb in 24h cooldown (Already Downloaded)")
            if e.code in (429, 500, 502, 503, 504) and a + 1 < tries:
                time.sleep(15 * (a + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def _nzb_search(artist: str, title: str, year: str = "") -> list[Candidate]:
    """Newznab search on nzb.life, audio only (cat 3000). The original
    release year is a relevance hint: scene titles carry the pressing
    year, so it keeps the original ahead of reissue/remaster packs.
    nzb.life ANDs free-text terms, so when no release title carries the
    year the yeared query returns nothing - fall back to the bare query
    (searches are free; only getnzb spends)."""
    cands = _nzb_query(f"{artist} {title}" + (f" {year}" if year else ""))
    if not cands and year:
        cands = _nzb_query(f"{artist} {title}")
    return cands


def _nzb_query(q: str) -> list[Candidate]:
    params = urllib.parse.urlencode(
        {"apikey": NZB_APIKEY, "t": "search", "q": q, "cat": "3000"})
    xml = _nzb_get(f"{NZB_API}/api?{params}", SEARCH_TIMEOUT).decode(
        "utf-8", "replace")
    cands: list[Candidate] = []
    for item in re.findall(r"<item>.*?</item>", xml, re.S):
        tm = re.search(r"<title>(.*?)</title>", item, re.S)
        gm = re.search(r"details/([0-9a-fA-F]+)", item)
        if not tm or not gm:
            continue
        pm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        posted = 0.0
        if pm:
            try:
                posted = parsedate_to_datetime(pm.group(1).strip()).timestamp()
            except (TypeError, ValueError):
                pass
        cands.append(Candidate(
            source="nzb",
            url=gm.group(1),
            title=html.unescape(tm.group(1).strip()),
            uploader="usenet",
            posted=posted,
        ))
    return cands


def search_candidates(source: Source, artist: str, title: str,
                      cookies: Path | None, year: str = "",
                      album: str = "") -> list[Candidate]:
    if source.kind == "archive":
        cands = _archive_search(artist, title, album)
    elif source.kind == "nzb":
        cands = _nzb_search(artist, title, year)
    else:
        cands = _yt_search(source.kind, artist, title, cookies)
    seen: set[str] = set()
    out: list[Candidate] = []
    for c in cands:
        if c.url in seen:
            continue
        seen.add(c.url)
        _score(c, artist)
        out.append(c)
    return out


def pick(cands: list[Candidate], artist: str, title: str) -> list[Candidate]:
    """Filter out censored/altered/foreign titles; rank best to worst.

    Ties break oldest-first: established usenet releases posted years ago
    are far more likely to be the genuine article than fresh uploads,
    whose file names and tags have proven unreliable."""
    bad = make_bad_filter(artist)
    usable = []
    for c in cands:
        if not title_present(c.title, title) or bad(c.title):
            continue
        # SoundCloud flat metadata reports the preview length (30s) for
        # preview-only tracks; the download precheck catches the rest.
        if c.source == "scsearch" and c.duration is not None and c.duration <= 30:
            continue
        usable.append(c)
    return sorted(usable, key=lambda c: (c.score, c.popularity, -c.posted),
                  reverse=True)
