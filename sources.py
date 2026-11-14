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

# How many 100-item pages an artist-only release browse pulls
# (nzb_artist_releases). 5 pages = 500 releases, enough for a prolific
# artist's whole audio catalog (313 for 'The Crystal Method'); the scan
# stops early on a short page, so a one-album artist costs one request.
NZB_ARTIST_PAGES = int(os.environ.get("TUNESTER_NZB_ARTIST_PAGES", "5"))

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
    "club mix", "dance mix", "extended mix", "ultra mix", "long version",
    "biomix",
)
NEVER_PARENS = re.compile(r"(?i)\((clean|radio\s*edit|censored)\)")


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    kind: str  # ytsearch | scsearch | archive | ytsearch_loose


# Best to worst: coverage x integrity x reliability.
# archive.org before SoundCloud: SoundCloud full tracks are DRM-protected
# (search works, downloads mostly error out) and the uploadable ones have
# proven to be covers/remixes/clean-titled karaoke - the Wil Bakx "Safety
# Dance" karaoke (2026-10-21) passed every text-only QC gate.
# Backburner (2026-09-26): YouTube is a poor source for uncensored studio
# originals, and this VPS IP gets bot-checked (the player response also
# needs the yt-dlp EJS JS-runtime signature solver; see yt-dlp/yt-dlp/wiki/EJS).
# To re-enable, restore these two entries and ship a JS runtime in the image:
#     Source("youtube", "YouTube", "ytsearch"),
#     Source("youtube-loose", "YouTube (deep)", "ytsearch_loose"),
SOURCES = [
    Source("nzb", "nzb.life (Usenet)", "nzb"),
    Source("archive", "archive.org", "archive"),
    Source("soundcloud", "SoundCloud", "scsearch"),
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
    release: str = ""    # archive.org item title (the collection this file
                         # was plucked from); the gate uses it to tell a
                         # studio album from a concert taper of the same track


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


# Candidate container preference: the standard CD/album track
# (an audio-native file) is tried before a music-video's audio track,
# which remains the fallback. Lossless (CD rip) ranks above lossy.
_LOSSLESS_EXTS = {"flac", "wav", "aif", "aiff", "ape", "tta", "wv"}
_VIDEO_EXTS = {"mkv", "mp4", "m4v", "mov", "avi", "wmv", "flv", "webm", "ogv"}


def _media_tier(url: str) -> int:
    """Preference tier for a candidate's container, read from its URL
    extension: 0 = lossless audio (CD rip), 1 = lossy audio or no
    extension (nzb IDs, soundcloud track URLs), 2 = video container.
    Only direct-file sources (archive.org) expose a real extension."""
    ext = Path(urllib.parse.urlparse(url).path).suffix.lower().lstrip(".")
    if ext in _LOSSLESS_EXTS:
        return 0
    if ext in _VIDEO_EXTS:
        return 2
    return 1


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


# archive.org item-level provenance. A per-venue concert taper (a dated
# "ob1995-10-20" / "*.aud" upload), a soundboard, or a radio/DJ rip is a
# LIVE recording, not the standard CD cut - but its per-file names carry
# the song title exactly like a rip ("339 Just Another Day.flac"), so the
# per-file filters cannot tell them apart and the tapers outrank studio
# items. Decide from the item identifier + title + creator ONLY: never
# the free-text description (every reissue mentions a concert) and never
# the separate date FIELD (a commercial album carries a YYYY-MM-DD release
# date there - Dead Man's Party 1985-10-28). A concert upload instead
# encodes its show date in its IDENTIFIER/title (taper convention), which
# no studio pressing does.
_DATE_ID = re.compile(r"\d{4}-\d{2}-\d{2}")
_LIVE_WORD = re.compile(r"(?i)\blive\b")
_SHOW_TOKEN = re.compile(
    r"(?i)full show|full concert|farewell show|set ?list|soundboard|"
    r"audience recording|dvd-?r\b|\btour\b|\btaper\b")
_VENUE = re.compile(
    r"(?i)theatre|theater|amphitheat(er|re)|palladium|warfield|aquarius|"
    r"irvine|euterpion|ballpark|house of blues|fillmore|olympia|civic|auditorium")
_RADIO_CREATOR = re.compile(
    r"(?i)\bradio\b|\bfm\b|\bdj\b|on ?air|airwaves|pop defect|prog ?rock")


def _archive_is_live_item(ident: str, title: str, creator: str,
                          artist_words: set[str]) -> bool:
    """True if the archive.org ITEM is a concert/soundboard/radio source.
    The whole item is then a live recording, so every file in it is
    dropped; the classifier never inspects individual files."""
    low_ident = ident.lower()
    stem = low_ident[:-4] if low_ident.endswith(".aud") else low_ident
    if low_ident.endswith(".aud") or _DATE_ID.search(stem):
        return True  # taper identifier: ob1995-10-20, ob1993-01-08.aud
    t = title.lower()
    if _SHOW_TOKEN.search(t):
        return True
    if "live" not in artist_words and _LIVE_WORD.search(t):
        return True  # a band named "Live" keeps its studio albums
    # A concert-hall/venue name in an audio ITEM title denotes a live
    # recording: a commercial studio album is never named after the hall.
    if _VENUE.search(t):
        return True
    # A show date in the title only means a taper with a venue or live/show
    # token alongside it; a reissue titled with a date alone is kept.
    if _DATE_ID.search(t) and (_VENUE.search(t) or _LIVE_WORD.search(t)
                               or _SHOW_TOKEN.search(t)):
        return True
    if _RADIO_CREATOR.search(creator):
        return True
    return False


def _archive_search(artist: str, title: str, album: str = "") -> list[Candidate]:
    """Audio items first; full-album uploads rarely mention individual
    track names in item metadata, so the album query lets the per-file
    title filter pluck the track. Video items (official music videos)
    are the fallback when no audio upload exists - the transcode step
    extracts the audio track, which for a video release is the studio
    recording. All queries merge; the per-file filters rank the rest."""
    queries = [f"{_query_text(artist)} {_query_text(title)} AND mediatype:(audio)"]
    if album:
        queries.append(f"{_query_text(artist)} {_query_text(album)} "
                       "AND mediatype:(audio)")
    queries.append(f"{_query_text(artist)} {_query_text(title)} "
                   "AND mediatype:(movies)")
    cands: list[Candidate] = []
    bad = make_bad_filter(artist)
    artist_words = set(_words(artist))
    want = _norm(title)
    for q in queries:
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
            item_title = str(md.get("title") or "").strip()
            if _archive_is_live_item(ident, item_title, uploader,
                                     artist_words):
                continue  # whole item is a concert/soundboard/radio source
            for f in meta.get("files") or []:
                name = f.get("name") or ""
                if "." not in name:
                    continue
                ext = name.rsplit(".", 1)[-1].lower()
                if ext not in ("mp3", "m4a", "ogg", "flac", "opus", "wav",
                               "mp4", "m4v", "mov", "avi", "mkv", "webm"):
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
                    release=item_title,
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


def _query_text(s: str) -> str:
    """Search-query form of an artist/title/album string: apostrophes
    (straight and typographic) removed. Both nzb.life and archive.org
    tokenize on the apostrophe, so "Dead Man's Party" ANDs into
    "Dead / Man / s / Party" and drowns in noise - it returns only the
    loosely-matched promo/bootleg docs and MISSES the clean scene rip
    titled "Dead Mans Party" and the studio album item titled
    "Oingo Boingo - Dead Man's Party" (proven: 3 noise docs vs the studio
    rips once stripped). MusicBrainz returns the curly form. The stored
    album keeps its apostrophe (it is only ever compared through
    _norm, which drops punctuation); this touches the query string only."""
    return s.replace("'", "").replace("\u2019", "")


def _nzb_search(artist: str, title: str, year: str = "") -> list[Candidate]:
    """Newznab search on nzb.life, audio only (cat 3000). The original
    release year is a relevance hint: scene titles carry the pressing
    year, so it keeps the original ahead of reissue/remaster packs.
    nzb.life ANDs free-text terms, so when no release title carries the
    year the yeared query returns nothing - fall back to the bare query
    (searches cost nothing; only getnzb fetches a manifest)."""
    cands = _nzb_query(f"{artist} {title}" + (f" {year}" if year else ""))
    if not cands and year:
        cands = _nzb_query(f"{artist} {title}")
    return cands


def _nzb_query(q: str, pages: int = 1) -> list[Candidate]:
    """Newznab audio search (cat 3000). The server caps every page at
    100 items (t=caps: max=100), so an artist-only browse of a prolific
    artist (313 releases for 'The Crystal Method') needs explicit
    paging: `pages` fetches offsets 0/100/200/... and stops early on a
    short page. The default single page keeps the normal artist+title
    search at its old single-request cost."""
    cands: list[Candidate] = []
    for off in range(0, max(1, pages) * 100, 100):
        params = {"apikey": NZB_APIKEY, "t": "search", "q": _query_text(q),
                  "cat": "3000"}
        if off:
            params["offset"] = off
        xml = _nzb_get(f"{NZB_API}/api?"
                       + urllib.parse.urlencode(params),
                       SEARCH_TIMEOUT).decode("utf-8", "replace")
        items = re.findall(r"<item>.*?</item>", xml, re.S)
        for item in items:
            tm = re.search(r"<title>(.*?)</title>", item, re.S)
            gm = re.search(r"details/([0-9a-fA-F]+)", item)
            if not tm or not gm:
                continue
            pm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
            posted = 0.0
            if pm:
                try:
                    posted = parsedate_to_datetime(
                        pm.group(1).strip()).timestamp()
                except (TypeError, ValueError):
                    pass
            cands.append(Candidate(
                source="nzb",
                url=gm.group(1),
                title=html.unescape(tm.group(1).strip()),
                uploader="usenet",
                posted=posted,
            ))
        if len(items) < 100:
            break
    return cands


def nzb_artist_releases(artist: str) -> list[Candidate]:
    """Every nzb.life audio release whose TITLE carries the artist name:
    the artist-credited albums AND the artist-titled comp/best-of boxes
    that no artist+album or artist+title query can reach (the comp title
    names neither the song nor the album - 'The Crystal Method - The
    Best Of + Vegas + Tweekend'). Paged past the 100-item page limit;
    the pipeline judges each by its NZB manifest, never by the title."""
    return _nzb_query(artist, pages=NZB_ARTIST_PAGES)


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

    The standard CD/album track (an audio-native file) outranks a
    music-video's audio track, and lossless outranks lossy. Ties break
    oldest-first: established usenet releases posted years ago are far
    more likely to be the genuine article than fresh uploads, whose
    file names and tags have proven unreliable."""
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
    return sorted(usable, key=lambda c: (-_media_tier(c.url), c.score,
                                         c.popularity, -c.posted),
                  reverse=True)
