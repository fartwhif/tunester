"""Core pipeline: dirty name -> verified uncensored MP3 (160 kbps CBR, 44.1 kHz, stereo).

Stages:
  1. discover   LLM (or heuristic) resolves the dirty name to artist/title/year
  2. search     sources best-to-worst, censored/altered uploads filtered out
  3. verify     ffprobe sanity + optional AcoustID content check + LLM gate
                that the download is the original uncensored studio recording
  4. transcode  ffmpeg -> MP3 160 kbps CBR / 44.1 kHz / stereo
  5. name       "ARTIST - TITLE (YEAR).mp3", output re-verified against spec
"""
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from llm import LLM, LLMError
from sources import (BROWSER_UA, HTTP_TIMEOUT, NNTP_HOST, NNTP_PASS,
                     NNTP_PORT, NNTP_USER, NZB_API, NZB_APIKEY, NZB_I,
                     SOURCES, YTDLP, Candidate, _norm, _nzb_get,
                     browser_user_agent, make_bad_filter, nzb_configured,
                     pick, search_candidates, title_present)

FFMPEG = os.environ.get("TUNESTER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("TUNESTER_FFPROBE", "ffprobe")
OUTDIR_DEFAULT = Path(os.environ.get("TUNESTER_OUTDIR") or Path.cwd() / "downloads")

# Two stores under one directory, neither of them under TUNESTER_OUTDIR: the
# delivered files in /data stay the user's downloads, this is pipeline state
# (compose binds the tunester-cache volume here; unset on a bare host ->
# ~/.cache/tunester).
#   deliveries/  the finished MP3, keyed on the song identity that was
#                verified: a later tune of that same song is copied straight
#                out and never searches or fetches again.
#   sources/     the verified SOURCE bytes, keyed on (source URL, fetched
#                file name): a later tune that selects the same candidate
#                skips the fetch - the slow, quota-spending part of a run
#                (the NNTP multi-part pull, yt-dlp, the archive.org GET).
# Sources are cached raw, never as a re-encode, so a hit re-runs the gates
# and the transcode and reproduces the delivery exactly.
CACHE_DEFAULT = Path(os.environ.get("TUNESTER_CACHE")
                     or Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
                     / "tunester")
CACHE_MAX_BYTES = int(os.environ.get("TUNESTER_CACHE_MAX_MB", "30720")) * 1024 * 1024
TRIALS_PER_SOURCE = int(os.environ.get("TUNESTER_TRIALS", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("TUNESTER_DOWNLOAD_TIMEOUT", "900"))

# Optional audio-content gate (needs TUNESTER_ACOUSTID_KEY): fingerprint the
# download with chromaprint and look it up on AcoustID (v2 API,
# api.acoustid.org/v2/lookup); a confident identification that is NOT the
# target song rejects the file - mislabeled articles (a "Loser" file
# containing a different track) pass every name/duration/LLM gate, so the
# content itself is checked. Confidence is 0..1 (v2 scale).
ACOUSTID_KEY = os.environ.get("TUNESTER_ACOUSTID_KEY", "")
ACOUSTID_HOST = os.environ.get("TUNESTER_ACOUSTID_HOST", "api.acoustid.org")
ACOUSTID_MIN_CONF = float(os.environ.get("TUNESTER_ACOUSTID_MIN_CONF", "0.7"))

COOKIES_CANDIDATES = [
    Path("/shared-cookies/cookies.txt"),  # container shared mount (compose)
    # host-side run: the sibling ytcookies export, relative to this repo
    Path(__file__).resolve().parent.parent / "ytcookies" / "data" / "cookies.txt",
    Path("data/cookies.txt"),
]


class TuneError(RuntimeError):
    """Pipeline failure. ``target`` carries the discovered artist/title/year
    when song discovery succeeded but no verified file could be delivered."""
    def __init__(self, msg, target=None):
        super().__init__(msg)
        self.target = target


@dataclass
class Target:
    artist: str
    title: str
    year: str = ""
    cd_year: str = ""  # first standard CD release year = the tag year
    album: str = ""
    mb_duration: float = 0.0  # seconds; 0.0 = no reliable anchor


@dataclass
class Result:
    artist: str
    title: str
    year: str
    source: str
    url: str
    file: Path
    gate: str  # "cache" | "probe" | "duration" | "acoustid" | "llm" | "filter-only"
    duration: float


@dataclass
class Options:
    outdir: Path = field(default_factory=lambda: OUTDIR_DEFAULT)
    cookies: Path | None = None
    source: str | None = None
    keep_source: bool = False
    use_llm: bool = True
    use_cache: bool = True


DISCOVER_SYSTEM = """You identify a single specific song from a user's rough, possibly dirty description
(misspellings, vague references, wrong artist names, slang).

Reply with ONLY a JSON object, no other text:
{"artist":"canonical artist name","title":"canonical song title",
 "year":"original studio release year as 4 digits, or empty string if unknown",
 "cd_year":"year the song first appeared on a standard commercial CD, as 4 digits, or empty string if unknown",
 "album":"name of the album the song first appeared on, or empty string if unknown"}

Rules:
- Use the canonical names as released; keep original casing.
- year is the original studio release year, not a remaster or cover year.
- cd_year is the FIRST standard commercial CD release of the song: for
  songs originally released on CD (mid-1980s onward) that is the
  original release year; for pre-CD songs it is the year the original
  album or single was first issued on CD - not a remaster, reissue,
  greatest-hits, remix or promo release. "" if genuinely unknown.
- album is the original album the song first appeared on (not a compilation,
  greatest-hits, or remix album); "" if it was a standalone single or unknown.
- If the input is already in "Artist - Title" form, parse it verbatim.
- If the input names an artist, that is the artist the user wants:
  normalize spelling and casing only, never re-attribute the song to a
  different artist, even if that artist does not appear to have a song
  by that title (a same-titled song by another artist is a different
  song).
- If the input names a title, that is the song the user wants: normalize
  spelling and casing only, never substitute a different (more famous)
  song by the same artist with a similar title.
- Do not substitute a different song. If the year is genuinely unknown, leave it ""."""

VERIFY_SYSTEM = """You are the QC gate of a music download pipeline. The pipeline must only deliver the
original, unaltered STUDIO recording of a target song, uncensored (the explicit release is what is wanted;
"(Explicit)" tags are good).

Reject (match=false) anything that is: a clean / explicit-removed edit, radio edit, live performance,
cover, remix, slowed/reverb, sped-up / nightcore, karaoke, instrumental, or lyric video.
A LIVE performance is never acceptable even when it is the correct song by the correct
artist and matches in length: the deliverable is the original uncensored studio
recording as it appears on the standard commercial CD. If the source is a live album,
a "Live at ..." release, or a concert/festival recording, match=false and version="live".
A music-video release is judged by its audio track (normally the studio recording);
scene tags (DDC, 1080p, x264, group names) describe the video, not the audio.
A multi-track release (12" single, sampler, compilation) is judged by the
specific source_file named below; if that file is a different track, match=false.
A source_collection names the album / upload the specific file was taken from: a dated
venue recording ("... 1993-01-08 ... The Warfield ...", a "*.aud" taper) or a "Live at ..."
upload is a concert even if the named file is the right song at the right length - match=false.

Reply with ONLY a JSON object, no other text:
{"match":true,"version":"studio|explicit|clean|radio|live|cover|remix|unknown","reason":"one short sentence"}

match=true ONLY when the downloaded item is the studio recording of exactly that song by exactly that artist.
When unsure, match=false."""


def find_cookies(explicit: Path | None) -> Path | None:
    for c in ([explicit] if explicit else []) + COOKIES_CANDIDATES:
        if c and c.exists() and c.stat().st_size > 0:
            return c
    return None


def _requested_artist(raw: str) -> str:
    """The artist the user explicitly typed in 'Artist - Title' form,
    or '' for free-form descriptions (where the LLM picks the artist)."""
    m = re.match(r"^\s*(?:the\s+)?(.+?)\s+[-\u2013\u2014:]\s+(.+?)\s*$", raw.strip())
    return m.group(1).strip() if m else ""


_TITLE_TAGS = re.compile(
    r"\s*\((?:hd|hq|hi[\s-]?res|official\s+music\s+video|official\s+video|"
    r"official|mv|video|full\s+song|full|1080p|720p|480p|4k|320\s*kbps|"
    r"256\s*kbps|192\s*kbps|128\s*kbps|320|256|128|mp3|flac|wav|aac|ogg|"
    r"m4a|stereo)\)\s*$", re.I)


def _requested_title(raw: str) -> str:
    """The title the user explicitly typed in 'Artist - Title' form,
    cleaned of a repeated artist prefix ("Gorillaz - Gorillaz - Feel Up"),
    a trailing year, and technical tags like '(HD)'. '' for free-form
    descriptions (where the LLM picks the title)."""
    m = re.match(r"^\s*(?:the\s+)?(.+?)\s+[-\u2013\u2014:]\s+(.+?)\s*$", raw.strip())
    if not m:
        return ""
    artist, title = m.group(1).strip(), m.group(2).strip()
    # A trailing "(1994)" is the year, not part of the title (the LLM
    # never includes it in its answer).
    title = re.sub(r"\s*\((1[9]\d{2}|20\d{2})\)\s*$", "", title)
    if artist:
        title = re.sub(rf"^{re.escape(artist)}\s+[-\u2013\u2014:]\s*",
                       "", title, flags=re.I)
    for _ in range(2):
        cleaned = _TITLE_TAGS.sub("", title)
        if cleaned == title:
            break
        title = cleaned
    return title.strip()


def _requested_year(raw: str) -> str:
    """The release year the user typed in 'Artist - Title (YYYY)', or ''.
    The trailing '(YYYY)' is the year, not part of the title (the same
    token _requested_title strips); a year in a free-form description
    has no such trailing group, so the LLM's year still applies there."""
    m = re.search(r"\((1[9]\d{2}|20\d{2})\)\s*$", raw.strip())
    return m.group(1) if m else ""


def _title_matches(a: str, b: str) -> bool:
    """Fuzzy title identity: misspellings and casing variants are the same
    song ('Feel Goud Inc' ~ 'Feel Good Inc.'); a different song never is
    ('Feel Up' !~ 'Feel Good Inc.'). Deliberately stricter than
    title_present (a recall-oriented candidate matcher): no containment
    shortcut, because song titles share words ('Summer' in 'Cruel
    Summer')."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def _artist_matches(a: str, b: str) -> bool:
    """Fuzzy artist identity: misspellings, casing, and punctuation
    variants are the same artist ('Rihana' ~ 'Rihanna'); a different
    artist never is ('Ace of Base' !~ 'Roy Orbison')."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.8


def discover(raw: str, llm: LLM, use_llm: bool, log=print) -> Target:
    raw = raw.strip()
    # Computed from the original raw before the heuristic branch below
    # strips a typed "(YYYY)" out of raw.
    req_year = _requested_year(raw)
    cd_year = ""
    if use_llm:
        d = llm.chat_json(DISCOVER_SYSTEM, raw)
        artist = str(d.get("artist") or "").strip()
        title = str(d.get("title") or "").strip()
        year = _clean_year(str(d.get("year") or ""))
        cd_year = _clean_year(str(d.get("cd_year") or ""))
        album = str(d.get("album") or "").strip()
        if not artist or not title:
            raise TuneError(f"LLM could not identify the song "
                            f"(artist={artist!r} title={title!r})")
        # The prompt licenses the LLM to fix "wrong artist names" and it
        # can re-attribute a song to a DIFFERENT artist with the same
        # title ("Ace of Base - Cruel Summer" -> "Roy Orbison - Cruel
        # Summer (1964)"); every downstream gate then targets the wrong
        # artist consistently and passes it. An explicitly typed artist
        # wins: pin it, and drop the LLM's year/cd_year/album (they
        # describe the substituted song) so the MB lookups below
        # re-derive them for the requested artist.
        req_artist = _requested_artist(raw)
        if req_artist and not _artist_matches(artist, req_artist):
            log(f"      LLM re-attributed '{artist}'; pinning requested "
                f"artist '{req_artist}'")
            artist, year, cd_year, album = req_artist, "", "", ""
        req_title = _requested_title(raw)
        if req_title and not _title_matches(title, req_title):
            # The artist swap one field over: the LLM substitutes a more
            # famous same-artist song ("Feel Up" -> "Feel Good Inc.");
            # every downstream gate then targets the wrong song
            # consistently and passes it. The typed title wins.
            log(f"      LLM substituted title '{title}'; pinning requested "
                f"title '{req_title}'")
            title, year, cd_year, album = req_title, "", "", ""
        # The typed release year is ground truth for the song the user
        # named. The LLM's year is nondeterministic - the same pinned
        # "Oingo Boingo - Just Another Day (1985)" query returned 1982 -
        # and a wrong year both zeroes the duration anchor (musicbrainz_-
        # duration needs a year-dated cluster) and empties the album-
        # derivation +/-1-year window, so the studio album-pluck and the
        # studio-length veto silently skip and only concert tapers remain.
        # The trailing "(YYYY)" is only ever present in the explicit
        # 'Artist - Title (YYYY)' form, so honoring it cannot touch a
        # free-form description (where the LLM's year still applies).
        if req_year:
            year = req_year
    else:
        # heuristic fallback: "Artist - Title (YYYY)"
        year = ""
        m = re.search(r"\((1[9]\d{2}|20\d{2})\)", raw)
        if m:
            year = m.group(1)
            raw = raw[: m.start()] + raw[m.end():]
        m = re.match(r"^\s*(?:the\s+)?(.+?)\s+[-\u2013\u2014:]\s+(.+?)\s*$", raw.strip())
        if not m:
            raise TuneError("cannot parse 'Artist - Title' without the LLM; "
                            "rerun without --no-llm")
        artist, title, album = m.group(1).strip(), m.group(2).strip(), ""
    # Without a typed year the only year driving musicbrainz_album's
    # +/-1 window and musicbrainz_duration's cluster is the discover LLM's
    # guess - nondeterministic (the same pinned "Oingo Boingo - Just
    # Another Day" returned 1982 AND 1990) and wrong often enough to
    # empty the album window so the studio rip (reachable ONLY through the
    # album query) is never found and a concert take wins instead. MB's
    # first-studio-album date is objective, so when the user typed no
    # year (GrabbyTube sends plain "Artist - Title"), trust it.
    if not req_year:
        mb_year = musicbrainz_studio_year(artist, title, log)
        if mb_year:
            if year and str(mb_year) != year:
                log(f"      search year {year} (LLM guess) -> {mb_year} "
                    f"(first studio album, MusicBrainz)")
            year = str(mb_year)
    if not album or _mb_release_exists(artist, album, log) is False:
        # An LLM album MusicBrainz refutes is often a label name or a
        # wrong guess (qwen said "Ruckus" for Men Without Hats); the
        # album query - how original-album CD rips are found - is only
        # as good as this string, so re-derive it from the MB tally.
        album = musicbrainz_album(artist, title, year, log) or album
    # The tag year is the first standard CD release year: CD-era rips
    # of a pre-CD album carry the CD pressing's date, not the studio
    # year (Men Without Hats "Rhythm of Youth": 1982 vinyl, 1984
    # Statik CD). A CD cannot predate the studio recording, so an
    # earlier answer is a slip and is dropped.
    if year and cd_year and int(cd_year) < int(year):
        cd_year = ""
    if not cd_year:
        cd_year = musicbrainz_cd_year(artist, title, log)
    # cd_year sits between year and album, so pass album BY NAME: a bare
    # positional 4th arg would land in cd_year (added 2026-10-22) and drop
    # the derived album - which is what made the studio-album pluck silently
    # stop running and every song fall back to concert tapers.
    target = Target(artist, title, year, album=album)
    target.cd_year = cd_year
    target.mb_duration = musicbrainz_duration(artist, title, year, log)
    if target.mb_duration:
        log(f"      duration anchor (MusicBrainz): {target.mb_duration:.0f}s")
    if cd_year and cd_year != year:
        log(f"      tag year (first standard CD): {cd_year}")
    return target


def _clean_year(s: str) -> str:
    m = re.search(r"(1[9]\d{2}|20\d{2})", s)
    return m.group(1) if m else ""


MB_BASE = "https://musicbrainz.org/ws/2"
MB_UA = "tunester/1.0 (music QC pipeline)"
MB_MIN_INTERVAL = 1.2          # MB's documented ~1 req/s budget per IP
_MB_LOCK = threading.Lock()
_MB_LAST = [0.0]               # wall time of the previous MB request

def _mb_get(path: str, log) -> dict | None:
    """One MusicBrainz Web Service call (JSON); None on any failure.
    Spaced >= MB_MIN_INTERVAL apart (discover chains an album + cd_year +
    duration lookup per tune and more across sources; firing them
    back-to-back self-inflicts a 429 that empties the album backfill and
    silently reverts to title-only search) and retried with backoff, so
    the studio-album anchor survives."""
    url = f"{MB_BASE}{path}"
    for attempt in range(4):
        with _MB_LOCK:
            wait = MB_MIN_INTERVAL - (time.time() - _MB_LAST[0])
            if wait > 0:
                time.sleep(wait)
            _MB_LAST[0] = time.time()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": MB_UA,
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < 4:
                time.sleep(1.5 * (attempt + 1))
                continue
            log(f"      musicbrainz: HTTP {e.code}")
            return None
        except Exception as e:
            log(f"      musicbrainz unreachable: {e.__class__.__name__}")
            return None
    return None


def _cluster_median(durs: list[float]) -> float:
    """Median of the largest within-15s cluster; 0.0 when <2 agree."""
    if len(durs) < 2:
        return 0.0
    durs = sorted(durs)
    best: list[float] = []
    for d in durs:
        cluster = [x for x in durs if abs(x - d) <= 15.0]
        if len(cluster) > len(best):
            best = cluster
    if len(best) < 2:
        return 0.0
    best = sorted(best)
    return float(best[len(best) // 2])

def musicbrainz_studio_year(artist: str, title: str,
                            log=print) -> int:
    """Year of the ORIGINAL studio album the song first appeared on, from
    MusicBrainz; 0 when it cannot be established.

    Among exact-title recordings (artist match enforced), a recording
    counts when it carries at least one NON-LIVE album release whose title
    differs from the song (a live album or a dated concert bootleg —
    "Boingo Alive", "13 January 1993, Universal Amphitheatre", "Farewell:
    Live from the Universal Amphitheater" — is not a studio album, and a
    release titled exactly like the song is the single, not the album).
    The earliest such recording's release date is the studio album year.
    The original album always predates its own compilations and live
    reissues, so earliest is the original, not a greatest-hits.

    This is ground truth for the search year when the user did not type
    one: MusicBrainz's date is exact, whereas the discover LLM's year is
    nondeterministic (the same pinned "Just Another Day" returned 1982 and
    1990) and a wrong year empties musicbrainz_album's +/-1 window AND
    mis-dates musicbrainz_duration's cluster, so the studio rip becomes
    unfindable and only concert takes remain — exactly the Oingo Boingo
    failure this fixes."""
    q = urllib.parse.urlencode({
        "query": f'recording:"{title}" AND artist:"{artist}"',
        "fmt": "json", "limit": 100, "inc": "release-groups"})
    data = _mb_get(f"/recording?{q}", log)
    if not data:
        return 0
    a, t = _norm(artist), _norm(title)
    best = 0
    for r in data.get("recordings", []):
        if _norm(str(r.get("title") or "")) != t:
            continue
        ac = _norm(" ".join(x.get("name", "")
                            for x in r.get("artist-credit", [])))
        if not (a in ac or ac in a):
            continue
        fd = str(r.get("first-release-date") or "")
        if not re.match(r"\d{4}", fd):
            continue
        year = int(fd[:4])
        if best and year >= best:
            continue  # only an earlier candidate can win
        has_studio_album = False
        for rel in r.get("releases", []):
            if _norm(str(rel.get("title") or "")) == t:
                continue  # the single, not the album
            rg = rel.get("release-group") or {}
            if str(rg.get("primary-type") or "").lower() == "live" or any(
                    str(s).lower() == "live"
                    for s in (rg.get("secondary-types") or [])):
                continue  # live album/bootleg
            has_studio_album = True
            break
        if has_studio_album:
            best = year
    return best



def musicbrainz_duration(artist: str, title: str, year: str,
                         log=print) -> float:
    """Canonical studio length (seconds) from MusicBrainz, 0.0 when it
    cannot be established.

    Only recordings whose title is exactly the target (no parenthesised
    version suffix) count - MB indexes every remix/edit as its own
    recording, and those outnumber the studio cut. With a known year the
    cluster must be dated within ±1/±3 of it: an undated or remix-era
    length is NOT the studio length, so the gate is skipped (0.0) rather
    than trust it."""
    q = urllib.parse.urlencode({
        "query": f'recording:"{title}" AND artist:"{artist}"',
        "fmt": "json", "limit": 30})
    data = _mb_get(f"/recording?{q}", log)
    if not data:
        return 0.0
    a, t = _norm(artist), _norm(title)
    dated: list[tuple[int | None, int]] = []
    for r in data.get("recordings", []):
        try:
            length = int(r.get("length") or 0) // 1000
        except (TypeError, ValueError):
            continue
        if length <= 30:
            continue
        if _norm(str(r.get("title") or "")) != t:
            continue
        ac = _norm(" ".join(x.get("name", "") for x in r.get("artist-credit", [])))
        if not (a in ac or ac in a):
            continue
        fd = str(r.get("first-release-date") or "")
        y = int(fd[:4]) if re.match(r"\d{4}", fd) else None
        dated.append((y, length))
    if not dated:
        return 0.0
    if year:
        ty = int(year)
        for w in (1, 3):
            m = _cluster_median([L for y, L in dated
                                 if y is not None and abs(y - ty) <= w])
            if m:
                return m
        return 0.0
    return _cluster_median([L for _, L in dated])


def _mb_release_exists(artist: str, album: str, log) -> bool | None:
    """True/False when MusicBrainz confirms/refutes a release titled
    `album` by `artist`; None on API failure (a failure is not a
    refutation - keep the LLM's album then)."""
    q = urllib.parse.urlencode({
        "query": f'release:"{album}" AND artist:"{artist}"',
        "fmt": "json", "limit": 10})
    data = _mb_get(f"/release?{q}", log)
    if data is None:
        return None
    a, t = _norm(artist), _norm(album)
    for r in data.get("releases", []):
        rt = _norm(str(r.get("title") or ""))
        ac = _norm(" ".join(x.get("name", "")
                            for x in r.get("artist-credit", [])))
        if rt == t and (a in ac or ac in a):
            return True
    return False


def musicbrainz_album(artist: str, title: str, year: str = "",
                      log=print) -> str:
    """Original album name from MusicBrainz, "" when unknown.

    The search API is nondeterministic about which recordings it returns
    and never reports release-group types, so selection is by a robust
    signal instead: the original album has BY FAR the most pressings in
    MB (worldwide reissues), while one-off compilations, radio promos
    and live bootlegs appear once or twice. Tally release titles across
    every exact-title recording match; releases titled exactly like the
    song are the single (whose 12" rips are the classic nzb false front -
    the VLS "Loser") and are excluded. With a known year, titles with a
    pressing dated within +/-1 of it are preferred (the album's own era -
    otherwise a later greatest-hits can out-press the original); the
    most-pressed title wins, ties by earliest date.

    A live-typed release-group - a live album or dated concert bootleg
    (MB marks even an "Album" primary-type with the "Live" secondary
    type) - is never the studio album a song "first appeared on", so it is
    dropped from the tally: Oingo Boingo's 30-pressing "13 January 1993,
    Universal Amphitheatre, Los Angeles" bootleg otherwise ties/outscores
    the real studio album (Bone / Nothing to Fear / Dark at the End of the
    Tunnel) inside a +/-1-year window around a mistyped 1994 and becomes
    the searched "album", so only concert rips are found."""
    q = urllib.parse.urlencode({
        "query": f'recording:"{title}" AND artist:"{artist}"',
        "fmt": "json", "limit": 100})
    data = _mb_get(f"/recording?{q}", log)
    if not data:
        return ""
    a, t = _norm(artist), _norm(title)
    display: dict[str, str] = {}   # norm title -> first-seen surface form
    earliest: dict[str, str] = {}  # norm title -> earliest date
    counts: dict[str, int] = {}    # norm title -> pressing count
    window: dict[str, int] = {}    # norm title -> pressings within year +/-1
    for r in data.get("recordings", []):
        if _norm(str(r.get("title") or "")) != t:
            continue
        ac = _norm(" ".join(x.get("name", "") for x in r.get("artist-credit", [])))
        if not (a in ac or ac in a):
            continue
        for rel in r.get("releases", []):
            rt = str(rel.get("title") or "").strip()
            if not rt:
                continue
            rg = rel.get("release-group") or {}
            if str(rg.get("primary-type") or "").lower() == "live" or any(
                    str(s).lower() == "live"
                    for s in (rg.get("secondary-types") or [])):
                continue  # a live album/bootleg is not the studio album
            n = _norm(rt)
            if n == t:
                continue
            display.setdefault(n, rt)
            counts[n] = counts.get(n, 0) + 1
            d = str(rel.get("date") or "")
            if d and (n not in earliest or d < earliest[n]):
                earliest[n] = d
            if year and re.match(r"\d{4}", d) \
                    and abs(int(d[:4]) - int(year)) <= 1:
                window[n] = window.get(n, 0) + 1
    if not counts:
        return ""
    keys = window.keys() if (year and window) else counts.keys()
    best = min(keys, key=lambda n: (-counts[n], earliest.get(n, "9999")))
    return display[best]


def musicbrainz_cd_year(artist: str, title: str, log=print) -> str:
    """Earliest standard CD release year from MusicBrainz, "" when
    unknown. Fallback when the LLM gives no cd_year: the recording
    search (inc=releases) lists every release carrying an exact-title
    match; a release counts when a medium has format CD and a dated
    official (else non-bootleg) status, and the earliest official year
    wins. MB's early-CD coverage is patchy (an early pressing can be a
    label-less stub, or missing entirely), so the LLM's answer is
    preferred when it has one."""
    q = urllib.parse.urlencode({
        "query": f'recording:"{title}" AND artist:"{artist}"',
        "fmt": "json", "limit": 100, "inc": "releases"})
    data = _mb_get(f"/recording?{q}", log)
    if not data:
        return ""
    a, t = _norm(artist), _norm(title)
    official, other = "", ""
    for r in data.get("recordings", []):
        if _norm(str(r.get("title") or "")) != t:
            continue
        ac = _norm(" ".join(x.get("name", "")
                            for x in r.get("artist-credit", [])))
        if not (a in ac or ac in a):
            continue
        for rel in r.get("releases", []):
            d = str(rel.get("date") or "")
            if not re.match(r"\d{4}", d):
                continue
            if not any(m.get("format") == "CD" for m in rel.get("media", [])):
                continue
            status = str(rel.get("status") or "").lower()
            if status == "bootleg":
                continue
            y = d[:4]
            if status == "official":
                official = y if not official or y < official else official
            else:
                other = y if not other or y < other else other
    return official or other


def probe(path: Path) -> dict | None:
    """ffprobe a file: duration / sample_rate / channels / bit_rate, or None."""
    cmd = [FFPROBE, "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        data = json.loads(proc.stdout.decode(errors="replace"))
    except Exception:
        return None
    audio = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
    if not audio:
        return None
    fmt = data.get("format") or {}
    dur = 0.0
    for src in (fmt.get("duration"), audio.get("duration")):
        try:
            dur = float(src)
            if dur > 0:
                break
        except (TypeError, ValueError):
            pass
    return {
        "duration": dur,
        "sample_rate": int(audio.get("sample_rate") or 0),
        "channels": int(audio.get("channels") or 0),
        "bit_rate": int(fmt.get("bit_rate") or 0),
    }


def _cache_dir(sub: str, log) -> Path | None:
    """One subtree of the cache (`sources` / `deliveries`), or None when the
    cache is unusable. A cache problem must never fail a tune."""
    try:
        d = CACHE_DEFAULT / sub
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError as e:
        log(f"        cache dir {CACHE_DEFAULT / sub} unavailable: {e}")
        return None


def _cache_key(url: str, name: str) -> str:
    """Key of one cached source file: sha1(source URL, fetched file name).

    A nzb release URL serves several different songs (album rips, 12"
    maxis), so the URL alone cannot identify bytes and the file's own name is
    part of the key. Single-file sources (archive.org direct link, SoundCloud
    /YouTube track page) pass name="", where the URL IS the whole identity.
    Unlike a song-name key, this one is exact: what is stored under a key is
    byte-identical to what that URL served, so re-running the gates and the
    transcode on it reproduces the delivery exactly."""
    return hashlib.sha1(f"{url}\x1f{name}".encode("utf-8")).hexdigest()


def _cache_read(d: Path, key: str) -> tuple[Path, dict] | None:
    """(blob, meta) of a cached source, or None. The JSON sidecar keeps the
    source URL, the fetched file's name and its extension: the name is what
    the QC gate judges on a multi-track release, and the extension is what
    the video/audio handling keys off, so both must survive the cache."""
    blob = d / f"{key}.bin"
    try:
        if blob.stat().st_size <= 1024:
            return None
        meta = json.loads((d / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return blob, meta


def _cache_hydrate(blob: Path, meta: dict, tmp: Path, log) -> Path | None:
    """Copy a cached blob into tmp as src.<ext>, so the pipeline works on a
    private file and can delete it (a rejected candidate is unlinked) without
    ever touching the cache entry."""
    ext = str(meta.get("ext") or "")
    dst = tmp / (f"src.{ext}" if ext else "src")
    try:
        shutil.copyfile(blob, dst)
        os.utime(blob, None)  # a hit refreshes the entry: LRU keeps what is used
    except OSError as e:
        log(f"        cache read failed: {e}")
        return None
    return dst


def _cache_hit(url: str, name: str, tmp: Path, log) -> Path | None:
    """Cached source bytes for (url, name) as a fresh file in tmp, or None
    when uncached. The copy is ffprobe-checked first, so a damaged entry
    costs a re-download instead of a bad delivery."""
    d = _cache_dir("sources", log)
    if d is None:
        return None
    rec = _cache_read(d, _cache_key(url, name))
    if rec is None:
        return None
    blob, meta = rec
    dst = _cache_hydrate(blob, meta, tmp, log)
    if dst is None:
        return None
    if not probe(dst):
        log("        cache entry not decodable - re-fetching")
        _cache_drop(d, _cache_key(url, name))
        dst.unlink(missing_ok=True)
        return None
    return dst


def _cache_release_hit(url: str, target: Target, tmp: Path,
                       log) -> tuple[Path, str] | None:
    """Cache hit for a whole-release pluck, where the plucked file's name is
    not yet known: scan the cache for a source recorded from this release URL
    whose name contains the target title - exactly the file the pluck would
    have selected. A different song from the same release is a miss."""
    d = _cache_dir("sources", log)
    if d is None:
        return None
    want = _norm(target.title)
    best: tuple[tuple[int, int], dict, Path] | None = None
    try:
        metas = list(d.glob("*.json"))
    except OSError:
        return None
    for mj in metas:
        key = mj.stem
        rec = _cache_read(d, key)
        if rec is None:
            continue
        blob, meta = rec
        name = str(meta.get("name") or "")
        if meta.get("url") != url or not want or want not in _norm(name):
            continue
        ext = str(meta.get("ext") or "")
        rank = ({"flac": 3, "wav": 2, "aiff": 2, "aif": 2,
                 "mp3": 1, "ogg": 1, "m4a": 1}.get(ext, 0),
                blob.stat().st_size)
        if best is None or rank > best[0]:
            best = (rank, meta, blob)
    if best is None:
        return None
    _rank, meta, blob = best
    name = str(meta.get("name") or "")
    dst = _cache_hydrate(blob, meta, tmp, log)
    if dst is None:
        return None
    if not probe(dst):
        log("        cache entry not decodable - re-fetching")
        _cache_drop(d, _cache_key(str(meta.get("url") or ""), name))
        dst.unlink(missing_ok=True)
        return None
    log(f"        download cache hit: {name} "
        f"({dst.stat().st_size // 1024} KB); whole-set NNTP fetch skipped")
    return dst, name


def _cache_drop(d: Path, key: str) -> None:
    for p in (d / f"{key}.bin", d / f"{key}.json"):
        try:
            p.unlink()
        except OSError:
            pass


def _blob_known(d: Path, key: str) -> bool:
    """True when one cache entry is already published under `key`."""
    try:
        return (d / f"{key}.bin").stat().st_size > 1024
    except OSError:
        return False


def _cache_write(d: Path, key: str, src: Path, meta: dict, log) -> bool:
    """Publish one entry (blob + JSON sidecar) under `key`; False when
    nothing was written.

    Blob and sidecar go to temp files in the cache dir and are os.replace'd
    onto their names: a run killed mid-copy, or two workers finishing the
    same song at once, can never expose a partial blob, and a reader never
    sees an entry half-mutated. A cache failure never fails the delivery."""
    blob, side = d / f"{key}.bin", d / f"{key}.json"
    if _blob_known(d, key):
        return False  # already cached
    tmp_blob = tmp_side = None
    try:
        fd, tmp_blob = tempfile.mkstemp(dir=str(d), prefix=key + ".",
                                        suffix=".blob.part")
        with os.fdopen(fd, "wb") as f:
            with open(src, "rb") as fh:
                shutil.copyfileobj(fh, f)
        os.chmod(tmp_blob, 0o644)
        fd, tmp_side = tempfile.mkstemp(dir=str(d), prefix=key + ".",
                                        suffix=".json.part")
        with os.fdopen(fd, "w") as f:
            json.dump(meta, f)
        os.replace(tmp_blob, blob)
        tmp_blob = None
        os.replace(tmp_side, side)
        tmp_side = None
    except OSError as e:
        log(f"        cache write failed: {e}")
        for stray in (tmp_blob, tmp_side):
            if stray:
                try:
                    os.unlink(stray)
                except OSError:
                    pass
        return False
    _cache_evict(log)
    return True


def _cache_put(path: Path, url: str, name: str, log) -> None:
    """Publish a verified source file under (url, name) so a later tune that
    selects the same candidate skips the fetch."""
    d = _cache_dir("sources", log)
    if d is None:
        return
    key = _cache_key(url, name)
    if _cache_write(d, key, path,
                    {"url": url, "name": name,
                     "ext": path.suffix.lstrip(".").lower()}, log):
        log(f"        cached source {name or path.name} "
            f"({(d / f'{key}.bin').stat().st_size // 1024} KB) -> {key[:12]}")


def _cache_evict(log) -> None:
    """Keep the cache under TUNESTER_CACHE_MAX_MB (default 30 GB), dropping
    the least-recently-used entries first. Both subtrees count against the
    cap and a hit refreshes an entry's mtime, so what is used survives.
    Source files are big (a FLAC album pluck is tens to hundreds of MB), so
    an unbounded store would eventually cost more than the fetches it
    saves."""
    try:
        entries = {b: [b, b.with_suffix(".json")]
                   for b in CACHE_DEFAULT.rglob("*.bin")}
        files = [p for v in entries.values() for p in v if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= CACHE_MAX_BYTES:
            return
        stale = sorted(
            entries.items(),
            key=lambda kv: max((p.stat().st_mtime for p in kv[1] if p.is_file()),
                               default=0.0))
    except OSError:
        return
    for blob, group in stale:
        group = [p for p in group if p.is_file()]
        freed = sum(p.stat().st_size for p in group)
        for p in group:
            try:
                p.unlink()
            except OSError:
                pass
        log(f"        cache over {CACHE_MAX_BYTES // (1024 * 1024)} MB: "
            f"evicted {blob.stem[:12]} ({freed // (1024 * 1024)} MB)")
        total -= freed
        if total <= CACHE_MAX_BYTES:
            return


def _delivery_key(target: Target) -> str:
    """Key of one verified delivery: sha1(normalized artist, normalized
    title) - the song identity the gates actually verified.

    The tag year is deliberately NOT part of it. Which year a pre-CD song
    first appeared on CD is fuzzy and discovery does not answer it the same
    way twice (observed: "Nena - 99 Luftballons" came back as 1983, 1984 and
    1986 inside an hour, and the same song landed in `downloads/` three
    times), so a year in the key would turn almost every repeat tune of a
    pre-CD song into a fresh download. The year still rides in the sidecar
    and is what the delivered file is named for (see `_delivery_get`)."""
    return hashlib.sha1("\x1f".join([_norm(target.artist), _norm(target.title)])
                        .encode("utf-8")).hexdigest()


def _delivery_name(meta: dict, target: Target) -> str:
    """Name a cached delivery is written out under: the name it was delivered
    with, so the file name and its ID3 date always agree. Falls back to this
    run's name for an entry whose sidecar has none."""
    name = str(meta.get("name") or "") or target_filename(target)
    return sanitize_name(Path(name).stem) + ".mp3"


def _delivery_get(target: Target, outdir: Path,
                  log) -> tuple[Path, dict, float] | None:
    """Copy the stored delivery of `target` into `outdir` and return
    (file, sidecar, duration), or None when uncached.

    The bytes land on a temp name in the output dir and are only moved into
    place once they pass the output spec again - so a corrupt or truncated
    entry costs a full re-tune instead of a bad delivery (and the bad entry
    is dropped). A hit writes the file the same way a fresh run would.
    The duration is also compared with this run's MusicBrainz anchor: a
    same-title entry that is a different cut (a 12" mix stored before the
    anchor existed) is a miss, not a substitution."""
    d = _cache_dir("deliveries", log)
    if d is None:
        return None
    key = _delivery_key(target)
    rec = _cache_read(d, key)
    if rec is None:
        return None
    blob, meta = rec
    dst = outdir / _delivery_name(meta, target)
    part = dst.with_name(dst.name + ".tunester-part")
    try:
        shutil.copyfile(blob, part)
    except OSError as e:
        log(f"        cache copy failed: {e}")
        part.unlink(missing_ok=True)
        return None
    problems = check_output(part, 0.0)
    dur = (probe(part) or {}).get("duration", 0.0)
    wrong_cut = (not problems and target.mb_duration and dur
                 and abs(dur - target.mb_duration) > 15.0)
    if problems or wrong_cut:
        log("        cached delivery rejected - re-tuning: "
            + ("; ".join(problems) if problems
               else f"cached copy is {dur:.0f}s, this run's "
                    f"MusicBrainz anchor is ~{target.mb_duration:.0f}s"))
        part.unlink(missing_ok=True)
        # Drop what the store cannot serve, so this run's verified delivery
        # replaces it: a spec failure in the blob, or a stored cut that this
        # run's duration anchor rules out, is no use to the next run either.
        # A damaged COPY of a sound blob is the one case to keep - the entry
        # is fine, only the read failed.
        if not problems or check_output(blob, 0.0):
            _cache_drop(d, key)
        return None
    try:
        os.replace(part, dst)
        os.utime(blob, None)  # a hit refreshes the entry: LRU keeps what is used
    except OSError as e:
        log(f"        cache copy failed: {e}")
        part.unlink(missing_ok=True)
        return None
    return dst, meta, dur


def _delivery_put(path: Path, target: Target, source: str, url: str,
                  log) -> bool:
    """Keep a copy of the finished MP3 under the song's identity, so the next
    tune of that song is served from the store instead of hunting for and
    re-encoding the recording all over again. True when a new entry was
    written."""
    d = _cache_dir("deliveries", log)
    if d is None:
        return False
    meta = {"name": path.name, "artist": target.artist, "title": target.title,
            "year": str(target.cd_year or target.year),
            "source": source, "url": url}
    if _cache_write(d, _delivery_key(target), path, meta, log):
        log(f"        cached delivery {path.name}")
        return True
    return False


DELIVERY_NAME_RE = re.compile(
    r"^(?P<artist>.+) - (?P<title>.+?)(?: \((?P<year>\d{4})\))?\.mp3$")


def seed_cache(outdir: Path, log=print) -> tuple[int, int, int]:
    """Copy an already-delivered MP3 library into the store so those songs
    never need fetching again. Only names that parse as the delivery name
    ("ARTIST - TITLE (YEAR).mp3") and files that still pass the output check
    are stored, so foreign MP3s in the output dir never enter the cache.
    Files whose song is already in the store are counted separately, so a
    second run says plainly that nothing new went in.
    Returns (stored, skipped, already-stored)."""
    d = _cache_dir("deliveries", log)
    if d is None:
        raise TuneError(f"cache dir {CACHE_DEFAULT / 'deliveries'} unusable")
    try:
        files = sorted(outdir.glob("*.mp3"))
    except OSError as e:
        raise TuneError(f"cannot read output dir {outdir}: {e}")
    stored = skipped = known = 0
    for f in files:
        m = DELIVERY_NAME_RE.match(f.name)
        if not m:
            skipped += 1
            log(f"  skip {f.name}: not an \"ARTIST - TITLE (YEAR).mp3\" name")
            continue
        target = Target(m["artist"], m["title"], year=m["year"] or "")
        if _blob_known(d, _delivery_key(target)):
            known += 1
            continue
        problems = check_output(f, 0.0)
        if problems:
            skipped += 1
            log(f"  skip {f.name}: " + "; ".join(problems))
            continue
        if _delivery_put(f, target, "", "", log):
            stored += 1
        else:
            skipped += 1
    return stored, skipped, known


def _archive_file_name(cand: Candidate) -> str:
    """File name an archive.org direct-link candidate serves, read from its
    URL path. Part of the cache identity so a put and its later lookup use the
    same key (the URL alone already identifies the bytes)."""
    return Path(urllib.parse.urlparse(cand.url).path).name


def download(cand: Candidate, tmp: Path, cookies: Path | None,
             target: Target, log, use_cache: bool = True) -> tuple[Path, str] | None:
    """Fetch a candidate into tmp (served from the download cache when it holds
    this source); return (file path, file name) or None. The file name is the
    per-file name the QC gate judges on a multi-track release, and is also the
    name part of the cache identity for that source."""
    if cand.source == "archive":
        name = _archive_file_name(cand)
        got = _download_http(cand, tmp, log, name, use_cache)
        return (got, name) if got else None
    if cand.source == "nzb":
        return _download_nzb(cand, tmp, target, log, use_cache)
    got = _download_yt(cand, tmp, cookies, log, use_cache)
    return (got, "") if got else None


def _download_yt(cand: Candidate, tmp: Path, cookies: Path | None, log,
                 use_cache: bool = True) -> Path | None:
    for old in tmp.glob("src.*"):
        old.unlink(missing_ok=True)
    # One URL is one track for yt-dlp, so the URL alone identifies it. A hit
    # also skips the simulate precheck: the preview-only/short-upload checks
    # are what qualified this blob for the cache in the first place.
    if use_cache:
        got = _cache_hit(cand.url, "", tmp, log)
        if got is not None:
            log(f"        download cache hit ({got.stat().st_size // 1024} KB)")
            return got
    flags = ["--no-playlist", "-f", "ba/b", "--no-write-thumbnail",
             "--no-write-info-json", "--no-write-subs", "--no-warnings"]
    if cand.source.startswith("yt") and cookies:
        flags += ["--cookies", str(cookies)]
        ua = browser_user_agent()
        if ua:
            flags += ["--user-agent", ua]
    # Precheck: what will yt-dlp actually deliver? Catches preview-only
    # uploads (SoundCloud) whose search metadata shows only the preview length.
    try:
        sim = subprocess.run(
            [YTDLP, *flags, "--simulate", "--print", "%(duration)s|%(format_id)s",
             "--", cand.url], capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        log("        precheck timed out")
        return None
    if sim.returncode != 0:
        err = sim.stderr.decode(errors="replace").strip().splitlines()
        log("        " + (err[-1] if err else f"precheck rc={sim.returncode}"))
        return None
    lines = sim.stdout.decode(errors="replace").strip().splitlines()
    if not lines:
        log("        precheck returned nothing")
        return None
    dur_str, _, fmt_id = lines[0].partition("|")
    if "preview" in fmt_id.lower():
        log(f"        preview-only upload ({fmt_id}); skipping")
        return None
    try:
        got = float(dur_str)
    except ValueError:
        got = 0.0
    if 0 < got < 45:
        log(f"        not a full track ({got:.0f}s); skipping")
        return None
    if cand.duration and got < 0.9 * float(cand.duration):
        log(f"        truncated source ({got:.0f}s of {cand.duration:.0f}s); skipping")
        return None
    out = tmp / "src.%(ext)s"
    cmd = [YTDLP, *flags, "--restrict-filenames", "-o", str(out), "--", cand.url]
    log(f"        downloading {cand.url}")
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("        download timed out")
        return None
    files = sorted(tmp.glob("src.*"))
    if proc.returncode != 0 or not files:
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        log("        " + (err[-1] if err else f"yt-dlp rc={proc.returncode}"))
        return None
    return files[0]


def _download_http(cand: Candidate, tmp: Path, log, name: str,
                   use_cache: bool = True) -> Path | None:
    for old in tmp.glob("src.*"):
        old.unlink(missing_ok=True)
    # One URL is one file on archive.org, so the URL alone identifies it; the
    # URL-derived name rides along as the (stable) identity name.
    if use_cache:
        got = _cache_hit(cand.url, name, tmp, log)
        if got is not None:
            log(f"        download cache hit ({got.stat().st_size // 1024} KB)")
            return got
    ext = Path(urllib.parse.urlparse(cand.url).path).suffix.lower() or ".mp3"
    dst = tmp / f"src{ext}"
    log(f"        downloading {cand.url}")
    try:
        req = urllib.request.Request(cand.url, headers={"User-Agent": "tunester/1.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                open(dst, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        log(f"        {e}")
        dst.unlink(missing_ok=True)
        return None
    return dst if dst.stat().st_size > 1024 else None


NZB_NS = "{http://www.newzbin.com/DTD/2003/nzb}"
NZB_AUDIO_EXTS = ("flac", "wav", "aiff", "aif", "mp3", "ogg", "m4a")
# Video containers whose audio track can be the studio recording (e.g. DDC
# music-video releases); only used when the release has no pure-audio file.
NZB_VIDEO_EXTS = ("mkv", "mp4", "m4v", "mov", "avi", "wmv", "flv", "webm")

# Archive containers that may wrap the media files (whole-release RAR/zip
# rips). When a release has no bare media file but has one of these, the
# whole set is fetched, par2-verified, extracted, and the track plucked.
NZB_ARCHIVE_EXTS = ("rar", "zip", "7z", "tar", "gz", "bz2", "xz", "tgz")
NZB_MAX_ARCHIVE = (int(os.environ.get("TUNESTER_NZB_MAX_ARCHIVE_MB", "1024"))
                   * 1024 * 1024)


def _nzb_file_name(subject: str) -> str:
    """Best-effort media file name from a part subject: a quoted
    filename, the longest (possibly nested) bracketed tag ending in a
    file extension, or the last token."""
    m = re.search(r'"([^"]+?\.[a-z0-9]{2,5})"', subject, re.I)
    if m:
        return m.group(1)
    best = ""
    for i, ch in enumerate(subject):
        if ch != "[":
            continue
        depth, j = 1, i + 1
        while j < len(subject) and depth:
            if subject[j] == "[":
                depth += 1
            elif subject[j] == "]":
                depth -= 1
            j += 1
        if depth:
            continue
        g = subject[i + 1:j - 1]
        if re.search(r"\.[a-z0-9]{2,5}$", g, re.I) and len(g) > len(best):
            best = g
    if best:
        return best
    parts = subject.split()
    return parts[-1] if parts else "file"


# Raw-8bit yEnc variant (WtFnZb-style): part bytes are stored shifted by
# +0x2a, and stored bytes in {NUL, TAB, LF, CR, SP, '.', '='} are written as
# '=' + (b + 0x40). The stored stream is therefore free of 0x00/0x0a/0x0d,
# which keeps the CRLF line structure unambiguous.
YENC8_SHIFT = 0x2A
YENC8_ESC = 0x3D
YENC8_ESC_OFF = 0x40


def _yenc8_decode(stored: bytes) -> bytes:
    out = bytearray()
    j = 0
    n = len(stored)
    while j < n:
        b = stored[j]
        if b == YENC8_ESC:
            j += 1
            if j >= n:
                raise ValueError("dangling yEnc escape")
            out.append((stored[j] - YENC8_ESC_OFF) & 0xFF)
        else:
            out.append(b)
        j += 1
    return bytes((b - YENC8_SHIFT) & 0xFF for b in out)


def _yenc_part(body: list[bytes]) -> bytes | None:
    """One NNTP article body (raw-8bit yEnc, =ybegin/=ypart/=yend markers)
    -> raw part bytes, or None on structure/CRC error."""
    try:
        yend = next(i for i, l in enumerate(body)
                    if l.lstrip().startswith(b"=yend"))
        m = re.search(rb"pcrc32=([0-9a-fA-F]{8})", body[yend])
        pcrc = int(m.group(1), 16) if m else None
        start = 0
        for i, l in enumerate(body):
            ls = l.lstrip()
            if ls.startswith(b"=ypart"):
                start = i + 1
                break
            if ls.startswith(b"=ybegin"):
                start = i + 1
        stored = b"".join(body[start:yend])
        part = _yenc8_decode(stored)
    except (StopIteration, ValueError, IndexError):
        return None
    if pcrc is not None and zlib.crc32(part) != pcrc:
        return None
    return part


def _nzb_cache_path(release_hash: str) -> Path | None:
    """Persistent .nzb cache path. A release's NZB is static, so getnzb
    runs once per release, not per run."""
    d = Path(os.environ.get("TUNESTER_NZB_CACHE", "/data/.nzb-cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / f"{release_hash}.nzb"


def _nzb_manifest(cand: Candidate, log) -> bytes | None:
    """Fetch a release's NZB manifest; served from the persistent cache
    when available, so getnzb runs once per release."""
    cp = _nzb_cache_path(cand.url)
    if cp is not None:
        try:
            if cp.exists() and cp.stat().st_size > 0:
                hit = cp.read_bytes()
                if hit.lstrip().startswith(b"<?xml"):
                    return hit
        except OSError:
            pass
    try:
        manifest = _nzb_get(
            f"{NZB_API}/getnzb/{cand.url}.nzb&i={NZB_I}&r={NZB_APIKEY}",
            HTTP_TIMEOUT)
    except Exception as e:
        log(f"        NZB manifest fetch failed: {e}")
        return None
    if cp is not None:
        try:
            cp.write_bytes(manifest)
        except OSError:
            pass
    return manifest


def _nzb_album_file(manifest: bytes, target: Target,
                    bad) -> str | None:
    """Name of a media file in the release whose name contains the song
    title (album pluck check), or None. Files flagged censored/altered
    (remix packs etc.) are ignored."""
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError:
        return None
    for fe in root.findall(f"{NZB_NS}file"):
        subj = fe.get("subject") or fe.findtext(f"{NZB_NS}subject") or ""
        name = _nzb_file_name(subj)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in NZB_AUDIO_EXTS + NZB_VIDEO_EXTS:
            continue
        if bad(name):
            continue
        if _norm(target.title) and _norm(target.title) in _norm(name):
            return name
    return None


def _nzb_album_cands(target: Target, seen: set[str],
                     log) -> list[Candidate]:
    """Extra nzb search by original album name + target year (so the
    original pressing outranks reissues); the first release whose NZB
    lists a clean media file matching the song title is returned (the
    track is then plucked at download). Up to 10 releases are NZB-checked
    (cached manifests are free); bad-curation false fronts - e.g. 2014
    reissues titled "(2001)" - can occupy the top ranks."""
    if not target.album:
        return []
    src = next(s for s in SOURCES if s.kind == "nzb")
    try:
        cands = search_candidates(src, target.artist, target.album, None,
                                  year=target.year, album=target.album)
    except Exception as e:
        log(f"      album search failed: {e}")
        return []
    bad = make_bad_filter(target.artist)

    def _rank(c):
        t = c.title.lower()
        return (target.album.lower() in t,
                "flac" in t or "wav" in t or "cd" in t,
                bool(target.year) and target.year in c.title,
                -c.posted)
    cands = sorted(cands, key=_rank, reverse=True)
    checked = 0
    for c in cands:
        if checked >= 10:
            break
        if c.url in seen or not _norm(target.album) in _norm(c.title):
            continue
        if bad(c.title):
            continue
        log(f"      pluck check {checked + 1}: {c.title!r}")
        manifest = _nzb_manifest(c, log)
        checked += 1
        if manifest is None:
            continue
        if _nzb_album_file(manifest, target, bad):
            log(f"      album pluck: song found in {c.title!r}")
            return [c]
    log("      album pluck: no release contained the track")
    return []


def _download_nzb(cand: Candidate, tmp: Path, target: Target,
                  log, use_cache: bool = True) -> tuple[Path, str] | None:
    """Fetch an nzb.life release over NNTP; return (path, file name),
    or None. A video file is used when the release has no pure-audio
    file (audio extracted at transcode)."""
    for old in tmp.glob("src.*"):
        old.unlink(missing_ok=True)
    manifest = _nzb_manifest(cand, log)
    if manifest is None:
        return None
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError as e:
        log(f"        bad NZB XML: {e}")
        return None
    files = []
    for fe in root.findall(f"{NZB_NS}file"):
        subj = fe.get("subject") or fe.findtext(f"{NZB_NS}subject") or ""
        grp = fe.findtext(f"{NZB_NS}groups/{NZB_NS}group") or ""
        segs = [(se.text.strip(), int(se.get("bytes", 0)))
                for se in fe.findall(f"{NZB_NS}segments/{NZB_NS}segment")]
        if segs:
            files.append((subj, grp, segs))
    audio = []
    video = []
    for subj, grp, segs in files:
        name = _nzb_file_name(subj)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in NZB_AUDIO_EXTS:
            audio.append((name, ext, grp, segs))
        elif ext in NZB_VIDEO_EXTS:
            video.append((name, ext, grp, segs))
    pool = audio or video
    if not pool:
        return _nzb_archive_pluck(files, cand.url, tmp, target, log, use_cache)

    bad = make_bad_filter(target.artist)
    ranked = []
    for item in pool:
        name, ext, _grp, segs = item
        if bad(name):
            continue
        size = sum(b for _, b in segs)
        hit = 1 if _norm(target.title) and _norm(target.title) in _norm(name) else 0
        fmt = {"flac": 3, "wav": 2, "aiff": 2, "aif": 2,
               "mp3": 1, "ogg": 1, "m4a": 1}.get(ext, 0)
        ranked.append((hit, fmt, size, item))
    if not ranked:
        log("        no usable audio file in release (censored/altered only)")
        return None
    top_hit = max(r[0] for r in ranked)
    # Never fetch a non-matching track when the release also lists a
    # matching one: 12" rips are named after their A-side, and the
    # B-sides are different songs.
    final = [r for r in ranked if r[0] == top_hit]
    _, _, _, (name, ext, grp, segs) = max(final, key=lambda r: (r[1], r[2]))
    if use_cache:
        got = _cache_hit(cand.url, name, tmp, log)
        if got is not None:
            log(f"        download cache hit: {name} "
                f"({got.stat().st_size // 1024} KB); NNTP fetch skipped")
            return got, name
    if ext in NZB_VIDEO_EXTS:
        log(f"        NNTP fetch: {name} ({len(segs)} parts; video, "
            f"audio only transcoded)")
    else:
        log(f"        NNTP fetch: {name} ({len(segs)} parts)")
    try:
        import nntplib  # stdlib; absent from Python 3.14+ hosts
        conn = nntplib.NNTP_SSL(NNTP_HOST, NNTP_PORT, user=NNTP_USER,
                                password=NNTP_PASS, timeout=30)
    except Exception as e:
        log(f"        NNTP connect failed: {e}")
        return None
    if grp:
        try:
            conn.group(grp)
        except Exception as e:
            log(f"        group select failed ({grp}): {e}")
            conn.quit()
            return None
    try:
        blob = b""
        for n, (mid, _enc) in enumerate(segs, 1):
            try:
                _resp, info = conn.body(f"<{mid}>")
            except Exception as e:
                log(f"        part {n}/{len(segs)} failed: {e}")
                return None
            part = _yenc_part(info.lines)
            if part is None:
                log(f"        part {n}/{len(segs)}: yEnc/CRC error")
                return None
            blob += part
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    if len(blob) < 1024:
        log("        decoded payload too small")
        return None
    out = tmp / f"src.{ext}"
    out.write_bytes(blob)
    return out, name


def _nzb_archive_pluck(files, url: str, tmp: Path, target: Target,
                       log, use_cache: bool = True) -> tuple[Path, str] | None:
    """The release has no bare media file: the audio is inside an archive
    (RAR/zip/7z) with recovery/parity files. Fetch the whole set over NNTP,
    verify (and repair) with par2 when present, extract with unar, then
    pluck the track with the same title-hit/format/size ranking as the
    bare-file path."""
    # Release-level cache: the plucked track is a deterministic function of
    # this release + the target title, so a track already plucked (and
    # verified) from this URL replays with identical bytes. The plucked file
    # name is only known after extraction, so the lookup matches on the
    # recorded release URL plus the same title-hit rule the pluck applies -
    # another song from the same album never reuses this entry.
    if use_cache:
        got = _cache_release_hit(url, target, tmp, log)
        if got is not None:
            return got
    total = sum(b for _s, _g, segs in files for _, b in segs)
    if total > NZB_MAX_ARCHIVE:
        log(f"        archive release too large: {total / 1e6:.0f} MB > "
            f"{NZB_MAX_ARCHIVE / 1e6:.0f} MB cap")
        return None
    log(f"        archive release: fetching whole set "
        f"({len(files)} files, ~{total / 1e6:.0f} MB)")
    arc = tmp / "arc"
    arc.mkdir(exist_ok=True)
    try:
        import nntplib  # stdlib; absent from Python 3.14+ hosts
        conn = nntplib.NNTP_SSL(NNTP_HOST, NNTP_PORT, user=NNTP_USER,
                                password=NNTP_PASS, timeout=30)
    except Exception as e:
        log(f"        NNTP connect failed: {e}")
        return None
    cur_grp = None
    try:
        for subj, grp, segs in files:
            if grp and grp != cur_grp:
                try:
                    conn.group(grp)
                except Exception as e:
                    log(f"        group select failed ({grp}): {e}")
                    return None
                cur_grp = grp
            blob = b""
            for n, (mid, _enc) in enumerate(segs, 1):
                try:
                    _resp, info = conn.body(f"<{mid}>")
                except Exception as e:
                    log(f"        part {n}/{len(segs)} failed: {e}")
                    return None
                part = _yenc_part(info.lines)
                if part is None:
                    log(f"        part {n}/{len(segs)}: yEnc/CRC error")
                    return None
                blob += part
            (arc / _nzb_file_name(subj)).write_bytes(blob)
    finally:
        try:
            conn.quit()
        except Exception:
            pass
    par2 = next(arc.glob("*.par2"), None)
    if par2 is not None and shutil.which("par2"):
        proc = subprocess.run(["par2", "v", str(par2)], cwd=arc,
                              capture_output=True, timeout=900)
        out = (proc.stdout or b"").decode(errors="replace")
        if "repair is not required" in out:
            log("        par2: all files correct")
        else:
            log("        par2: problems found - repairing")
            subprocess.run(["par2", "r", str(par2)], cwd=arc,
                           capture_output=True, timeout=3600)
    archives = sorted(
        (p for p in arc.iterdir()
         if p.is_file()
         and p.name.rsplit(".", 1)[-1].lower() in NZB_ARCHIVE_EXTS),
        key=lambda p: p.name.lower())
    if not archives:
        log("        no archive file in release")
        return None
    primary = (next((p for p in archives
                     if p.name.lower().endswith(".part1.rar")), None)
               or next((p for p in archives if p.suffix.lower() == ".rar"),
                       archives[0]))
    if not shutil.which("unar"):
        log("        unar not installed; cannot extract archive")
        return None
    outdir = tmp / "arc_x"
    outdir.mkdir(exist_ok=True)
    proc = subprocess.run(["unar", "-f", "-o", str(outdir), str(primary)],
                          capture_output=True, timeout=3600)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        log(f"        extraction failed: {err[-1] if err else 'unknown'}")
        return None
    audio = [p for p in outdir.rglob("*")
             if p.is_file() and p.suffix.lower().lstrip(".") in NZB_AUDIO_EXTS]
    if not audio:
        log("        no audio file in archive")
        return None
    bad = make_bad_filter(target.artist)
    ranked = []
    for p in audio:
        if bad(p.name):
            continue
        ext = p.suffix.lower().lstrip(".")
        fmt = {"flac": 3, "wav": 2, "aiff": 2, "aif": 2,
               "mp3": 1, "ogg": 1, "m4a": 1}.get(ext, 0)
        hit = (1 if _norm(target.title) and _norm(target.title)
               in _norm(p.name) else 0)
        ranked.append((hit, fmt, p.stat().st_size, p))
    if not ranked:
        log("        no usable audio file in archive (censored/altered only)")
        return None
    top_hit = max(r[0] for r in ranked)
    final = [r for r in ranked if r[0] == top_hit]
    _, _, _, best = max(final, key=lambda r: (r[1], r[2]))
    log(f"        archive pluck: {best.name}")
    return best, best.name


def _year_mismatch(title: str, year: str) -> bool:
    """True when the release title carries a concrete year more than 5
    years from the target year (a different era - e.g. a 2014 remix pack
    for a 2001 song, or a reissue whose tracklist changed). Single
    pressings within 5 years are the same studio cut, so they pass.
    Year-less titles are never rejected by this."""
    if not re.fullmatch(r"(19|20)\d{2}", year or ""):
        return False
    for m in re.finditer(r"(19\d{2}|20\d{2})", title):
        if abs(int(m.group(1)) - int(year)) > 5:
            return True
    return False


@lru_cache(maxsize=4)
def _consensus_duration(artist: str, title: str) -> float | None:
    """Duration the other sources most agree on for this song
    (previews excluded); None when no sources agree. The canonical
    version is the length most uploads share, not the overall
    median - remixes and edits can dominate a search."""
    durs = []
    for src in SOURCES:
        if src.kind == "nzb":
            continue
        try:
            for c in search_candidates(src, artist, title, None):
                if c.duration and c.duration > 45:
                    durs.append(float(c.duration))
        except Exception:
            pass
    durs.sort()
    best = []
    for d in durs:
        cluster = [x for x in durs if abs(x - d) <= 15.0]
        if len(cluster) > len(best):
            best = cluster
    if len(best) < 2:
        return None
    best.sort()
    return best[len(best) // 2]


def _acoustid_identify(path: Path, target: Target) -> tuple[str, str, float] | None:
    """Fingerprint `path` (chromaprint) and look it up on AcoustID (v2).
    Returns (artist, title, confidence 0..1) for the best match, or None
    when no key is set, fpcalc is missing, or the service fails.

    The best result is a cluster of acoustically similar recordings. If
    ANY member matches the target, the target match is returned: a
    misattributed copy can rank first in the cluster (the genuine
    Men Without Hats "The Safety Dance" was outranked by a "SIRCH -
    Safety Dance" entry for the same audio, 2026-10-21)."""
    if not ACOUSTID_KEY or not shutil.which("fpcalc"):
        return None
    try:
        proc = subprocess.run(["fpcalc", str(path)],
                              capture_output=True, timeout=300)
        if proc.returncode != 0:
            return None
        # fpcalc prints "FINGERPRINT=..." / "DURATION=..." text lines
        kv = {}
        for line in (proc.stdout or b"").decode(errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
        fp, dur = kv.get("FINGERPRINT", ""), int(kv.get("DURATION", "0"))
        if not fp or dur <= 0:
            return None
    except (OSError, ValueError):
        return None
    body = urllib.parse.urlencode({
        "client": ACOUSTID_KEY, "fingerprint": fp, "duration": str(dur),
        "meta": "recordings", "format": "json"})
    try:
        req = urllib.request.Request(
            f"https://{ACOUSTID_HOST}/v2/lookup",
            data=body.encode("utf-8"),
            headers={"User-Agent": BROWSER_UA,
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except Exception:
        return None
    results = d.get("results") or []
    if not results:
        return None
    top = results[0]
    conf = float(top.get("score") or 0.0)
    recs = top.get("recordings") or []
    if not recs:
        return None
    for rec in recs:
        artists = rec.get("artists") or []
        artist = str(artists[0].get("name") or "") if artists else ""
        title = str(rec.get("title") or "")
        if title and _acoustid_matches(artist, title, target):
            return artist, title, conf
    rec = recs[0]
    artists = rec.get("artists") or []
    artist = str(artists[0].get("name") or "") if artists else ""
    title = str(rec.get("title") or "")
    if not title:
        return None
    return artist, title, conf


def _acoustid_matches(artist: str, title: str, target: Target) -> bool:
    if not artist or not title:
        return False
    artist_ok = (_norm(target.artist) in _norm(artist)
                 or _norm(artist) in _norm(target.artist))
    return artist_ok and (title_present(title, target.title)
                          or title_present(target.title, title))


def verify_download(path: Path, cand: Candidate, target: Target,
                    llm: LLM, use_llm: bool, log,
                    file_name: str = "") -> tuple[bool, str, str]:
    """(ok, gate, reason). gate: 'probe' | 'duration' | 'acoustid' | 'llm'
    | 'filter-only'."""
    info = probe(path)
    if not info:
        return False, "probe", "no decodable audio stream"
    if info["duration"] < 20:
        return False, "probe", f"too short ({info['duration']:.0f}s) - preview or clip"
    if cand.duration and info["duration"] < 0.9 * float(cand.duration):
        return False, "probe", (
            f"truncated: {info['duration']:.0f}s of {cand.duration:.0f}s "
            "(preview or partial upload)")
    # Duration anchor: the length the target STUDIO recording is expected
    # to have, applied to EVERY candidate - not only self-report-free
    # ones. An archive.org concert taper carries its OWN tracklen, so
    # gating this on `not cand.duration` let a 362s ob1995 taper clear a
    # 314s studio target: it compared 362s against the taper's own 362s
    # and never against the studio length. cand.duration's only job is
    # the truncation check just above. With a known year only the
    # year-dated MusicBrainz length is trusted (a remix-era length must
    # not veto the studio cut), so a 0 mb_duration means "no anchor",
    # not a veto.
    if target.year:
        expect = target.mb_duration
        why = "MusicBrainz"
    else:
        expect = target.mb_duration or _consensus_duration(
            target.artist, target.title)
        why = "MusicBrainz/other sources"
    if expect and abs(info["duration"] - expect) > 15.0:
        return False, "duration", (
            f"{info['duration']:.0f}s vs ~{expect:.0f}s per {why}")
    # Objective facts the LLM cannot see, recorded here for the version
    # veto below: whether the length agrees with the studio anchor, and
    # (set in the AcoustID block) whether the fingerprint proves a clean
    # match to the studio recording rather than a live take.
    anchored = bool(expect and abs(info["duration"] - expect) <= 15.0)
    studio_confirmed = False
    ident = _acoustid_identify(path, target)
    if ident is not None:
        id_artist, id_title, conf = ident
        if conf >= ACOUSTID_MIN_CONF:
            if not _acoustid_matches(id_artist, id_title, target):
                return False, "acoustid", (
                    f"fingerprint identifies as {id_artist} - {id_title} "
                    f"(conf {conf * 100:.0f}%)")
            # A fingerprint that identifies the content as a LIVE version
            # of the target still passes _acoustid_matches (the target
            # title is a substring of the live title) - reject it: the
            # deliverable is the studio CD recording, and a live take of
            # the same song is not. A target that is itself a live
            # recording (rare) is exempt.
            if re.search(r"\blive\b", id_title, re.I) and not re.search(
                    r"\blive\b", target.title, re.I):
                return False, "acoustid", (
                    f"fingerprint identifies a LIVE version "
                    f"({id_artist} - {id_title}, conf {conf * 100:.0f}%)")
            log(f"        acoustid: {id_artist} - {id_title} "
                f"(conf {conf * 100:.0f}%) - matches target")
            studio_confirmed = True
        else:
            log(f"        acoustid: best conf {conf * 100:.0f}% < "
                f"{ACOUSTID_MIN_CONF * 100:.0f}% - gate skipped")

    if not (use_llm and llm):
        return True, "filter-only", ""
    extra = f'\nsource_file="{file_name}"' if file_name else ""
    # The archive item title (the collection the file was plucked from)
    # disambiguates a studio album rip from a same-song, same-length
    # concert taper whose per-file name is just "Skin".
    if cand.release and _norm(cand.release) != _norm(cand.title):
        extra += f'\nsource_collection="{cand.release}"'
    if path.suffix.lstrip(".").lower() in NZB_VIDEO_EXTS:
        extra += "\nsource=video release (only the audio track is judged)"
    user = (
        f'target_artist="{target.artist}"\n'
        f'target_title="{target.title}"\n'
        f'downloaded_title="{cand.title}"\n'
        f'downloaded_uploader="{cand.uploader}"\n'
        f"downloaded_duration_sec={info['duration']:.0f}"
        + extra
    )
    try:
        d = llm.chat_json(VERIFY_SYSTEM, user)
    except LLMError as e:
        log(f"        LLM gate unavailable ({e}); proceeding filter-only")
        return True, "filter-only", ""
    version = str(d.get("version") or "unknown")
    # A 'live' claim is the one veto an objective fingerprint can refute,
    # and it is checked BEFORE the match flag: the QC prompt tells the
    # model to answer "match=false AND version=live" for a live album, so
    # a name-based live claim almost always arrives with match=false and
    # would otherwise die on the match flag below. The AcoustID block
    # above only sets studio_confirmed for a high-confidence fingerprint
    # match to a NON-live recording (it rejects a live-identified one
    # outright), so studio_confirmed + anchored is content proof of the
    # studio cut the audio-blind LLM cannot see - it invented 'live' from
    # the file name for MusicBrainz-Studio album rips (Oingo Boingo "Dark
    # at the End of the Tunnel") at 96-98% fingerprint confidence. A
    # genuine live take fails the length anchor and is never confirmed,
    # so this overrule only rescues the fingerprint-verified studio rip.
    if version == "live" and studio_confirmed and anchored:
        log(f"        acoustid ({id_artist} - {id_title}, "
            f"conf {conf * 100:.0f}%) confirms the studio recording; "
            f"overriding LLM '{version}' veto")
        return True, "acoustid", (
            "fingerprint + studio-anchor length confirm the studio "
            f"recording; LLM '{version}' veto overruled")
    if not d.get("match"):
        return False, "llm", f"{version}: {d.get('reason', 'rejected')}"
    if version in ("clean", "radio", "live", "cover", "remix"):
        # The match flag is not trusted on its own: the LLM sometimes
        # answers match=true while labeling the version live/cover/
        # remix. Only the studio CD recording is a deliverable, so the
        # version vocabulary is enforced regardless of the match flag.
        return False, "llm", (
            f"{version}: " + (d.get("reason") or "not a studio recording"))
    return True, "llm", ""


def transcode(src: Path, dst: Path, target: Target) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-c:a", "libmp3lame", "-b:a", "160k", "-ar", "44100", "-ac", "2",
           "-write_xing", "1",
           "-metadata", f"title={target.title}",
           "-metadata", f"artist={target.artist}"]
    tag_year = target.cd_year or target.year
    if tag_year:
        cmd += ["-metadata", f"date={tag_year}"]
    cmd += [str(dst)]
    proc = subprocess.run(cmd, capture_output=True, timeout=DOWNLOAD_TIMEOUT)
    if proc.returncode != 0 or not dst.exists():
        err = proc.stderr.decode(errors="replace").strip().splitlines()
        raise TuneError("transcode failed: " + (err[-1] if err else "unknown error"))


def check_output(mp3: Path, src_dur: float) -> list[str]:
    """Re-verify the final MP3 meets the delivery spec."""
    p = probe(mp3)
    if not p:
        return ["output: no decodable audio stream"]
    problems = []
    if p["sample_rate"] != 44100:
        problems.append(f"output: sample_rate {p['sample_rate']} != 44100")
    if p["channels"] != 2:
        problems.append(f"output: channels {p['channels']} != 2")
    if not (150_000 <= p["bit_rate"] <= 170_000):
        problems.append(f"output: bit_rate {p['bit_rate']} not ~160k CBR")
    if src_dur and abs(p["duration"] - src_dur) > max(3.0, 0.05 * src_dur):
        problems.append(
            f"output: duration {p['duration']:.0f}s vs source {src_dur:.0f}s")
    proc = subprocess.run([FFMPEG, "-v", "error", "-f", "null", "-", "-i", str(mp3)],
                          capture_output=True, timeout=DOWNLOAD_TIMEOUT)
    if proc.returncode != 0:
        problems.append("output: full-decode check failed")
    return problems


def sanitize_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:180].rstrip(" .") or "untitled"


def target_filename(target: Target) -> str:
    name = f"{target.artist} - {target.title}"
    tag_year = target.cd_year or target.year
    if tag_year:
        name += f" ({tag_year})"
    return sanitize_name(name) + ".mp3"


def tune(raw: str, opts: Options | None = None, llm: LLM | None = None,
         log=print) -> Result:
    """Run the full pipeline. Returns a Result or raises TuneError."""
    opts = opts or Options()
    llm = llm if llm is not None else LLM(log=log)
    cookies = find_cookies(opts.cookies)

    log(f"[1/5] discover: {raw!r}")
    target = discover(raw, llm, opts.use_llm, log)
    tag_year = target.cd_year or target.year
    log(f"      -> {target.artist} - {target.title}"
        + (f" ({tag_year})" if tag_year else " (year unknown)")
        + (f" [{target.album}]" if target.album else ""))

    outdir = opts.outdir
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise TuneError(f"cannot create output dir {outdir}: {e}", target=target)
    dst = outdir / target_filename(target)

    # Already in the store? Then this song was found, verified and encoded
    # once: hand the same file back and skip the search, the fetch and the
    # transcode. The stored name wins over this run's guess at the tag year
    # (the key is artist+title precisely because that year is fuzzy), and the
    # copy is re-checked against the output spec before it lands anywhere.
    if opts.use_cache:
        hit = _delivery_get(target, outdir, log)
        if hit is not None:
            dst, meta, dur = hit
            log(f"[2/5] cache hit: {target.artist} - {target.title} is already "
                f"in the store - no search, no fetch, no transcode")
            log(f"      from {meta.get('source') or 'a previous run'}: "
                f"{meta.get('url') or ''}")
            log(f"[5/5] done -> {dst}")
            return Result(target.artist, target.title,
                          str(meta.get("year") or tag_year),
                          str(meta.get("source") or ""),
                          str(meta.get("url") or ""), dst, "cache", dur)

    tmp = Path(tempfile.mkdtemp(prefix="tunester-"))
    sources = [s for s in SOURCES if not opts.source or s.id == opts.source]

    try:
        for src in sources:
            log(f"[2/5] source: {src.label}")
            if src.kind == "nzb" and not nzb_configured():
                log("      not configured (TUNESTER_NZB_APIKEY/TUNESTER_NNTP_*); skipping")
                continue
            try:
                cands = search_candidates(src, target.artist, target.title,
                                          cookies, year=target.year,
                                          album=target.album)
            except Exception as e:
                log(f"      search failed: {e}")
                continue
            usable = pick(cands, target.artist, target.title)
            if target.year:
                usable = [c for c in usable
                          if not _year_mismatch(c.title, target.year)]
            log(f"      {len(cands)} found, {len(usable)} usable "
                f"after censored/altered filter")
            if src.kind == "nzb":
                # Pluck candidate first: the original album is the
                # canonical source and its candidate is already
                # manifest-verified to contain the track.
                usable = _nzb_album_cands(
                    target, {c.url for c in cands}, log) + usable
            if not usable:
                continue
            for cand in usable[:TRIALS_PER_SOURCE]:
                log(f"      try: {cand.title!r} <- {cand.uploader or '?'}")
                try:
                    got = download(cand, tmp, cookies, target, log, opts.use_cache)
                except Exception as e:
                    log(f"      download failed: {e}")
                    continue
                if got is None:
                    continue
                path, fname = got
                ok, gate, why = verify_download(path, cand, target, llm,
                                                opts.use_llm, log, fname)
                if not ok:
                    log(f"      REJECTED [{gate}]: {why}")
                    path.unlink(missing_ok=True)
                    continue
                log(f"[3/5] verified uncensored studio recording [{gate}]")
                log("[4/5] transcode -> MP3 160 kbps CBR / 44.1 kHz / stereo")
                try:
                    transcode(path, dst, target)
                except TuneError as e:
                    log(f"      {e}")
                    path.unlink(missing_ok=True)
                    continue
                info = probe(path) or {}
                problems = check_output(dst, info.get("duration", 0.0))
                if problems:
                    log("      " + "; ".join(problems))
                    dst.unlink(missing_ok=True)
                    path.unlink(missing_ok=True)
                    continue
                if opts.use_cache:
                    # Only what passed every gate is stored, so neither store
                    # ever accumulates previews or mislabeled junk.
                    _cache_put(path, cand.url, fname, log)
                    _delivery_put(dst, target, cand.source, cand.url, log)
                out_info = probe(dst) or {}
                log(f"[5/5] done -> {dst}")
                return Result(target.artist, target.title,
                              target.cd_year or target.year,
                              cand.source, cand.url, dst, gate,
                              out_info.get("duration", 0.0))
        raise TuneError("all sources exhausted - no verified uncensored copy found",
                        target=target)
    finally:
        if not opts.keep_source:
            shutil.rmtree(tmp, ignore_errors=True)
