"""Core pipeline: dirty name -> verified uncensored MP3 (160 kbps CBR, 44.1 kHz, stereo).

Stages:
  1. discover   LLM (or heuristic) resolves the dirty name to artist/title/year
  2. search     sources best-to-worst, censored/altered uploads filtered out
  3. verify     ffprobe sanity + LLM gate that the download is the original
                uncensored studio recording
  4. transcode  ffmpeg -> MP3 160 kbps CBR / 44.1 kHz / stereo
  5. name       "ARTIST - TITLE (YEAR).mp3", output re-verified against spec
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
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
                     pick, search_candidates)

FFMPEG = os.environ.get("TUNESTER_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("TUNESTER_FFPROBE", "ffprobe")
OUTDIR_DEFAULT = Path(os.environ.get("TUNESTER_OUTDIR") or Path.cwd() / "downloads")
TRIALS_PER_SOURCE = int(os.environ.get("TUNESTER_TRIALS", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("TUNESTER_DOWNLOAD_TIMEOUT", "900"))

COOKIES_CANDIDATES = [
    Path("/shared-cookies/cookies.txt"),                    # container shared mount
    Path("/home/user/projects/ytcookies/data/cookies.txt"),  # host-side export
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
    gate: str  # "probe" | "duration" | "llm" | "filter-only"
    duration: float


@dataclass
class Options:
    outdir: Path = field(default_factory=lambda: OUTDIR_DEFAULT)
    cookies: Path | None = None
    source: str | None = None
    keep_source: bool = False
    use_llm: bool = True


DISCOVER_SYSTEM = """You identify a single specific song from a user's rough, possibly dirty description
(misspellings, vague references, wrong artist names, slang).

Reply with ONLY a JSON object, no other text:
{"artist":"canonical artist name","title":"canonical song title",
 "year":"original studio release year as 4 digits, or empty string if unknown",
 "album":"name of the album the song first appeared on, or empty string if unknown"}

Rules:
- Use the canonical names as released; keep original casing.
- year is the original studio release year, not a remaster or cover year.
- album is the original album the song first appeared on (not a compilation,
  greatest-hits, or remix album); "" if it was a standalone single or unknown.
- If the input is already in "Artist - Title" form, parse it verbatim.
- Do not substitute a different song. If the year is genuinely unknown, leave it ""."""

VERIFY_SYSTEM = """You are the QC gate of a music download pipeline. The pipeline must only deliver the
original, unaltered STUDIO recording of a target song, uncensored (the explicit release is what is wanted;
"(Explicit)" tags are good).

Reject (match=false) anything that is: a clean / explicit-removed edit, radio edit, live performance,
cover, remix, slowed/reverb, sped-up / nightcore, karaoke, instrumental, or lyric video.
A music-video release is judged by its audio track (normally the studio recording);
scene tags (DDC, 1080p, x264, group names) describe the video, not the audio.
A multi-track release (12" single, sampler, compilation) is judged by the
specific source_file named below; if that file is a different track, match=false.

Reply with ONLY a JSON object, no other text:
{"match":true,"version":"studio|explicit|clean|radio|live|cover|remix|unknown","reason":"one short sentence"}

match=true ONLY when the downloaded item is the studio recording of exactly that song by exactly that artist.
When unsure, match=false."""


def find_cookies(explicit: Path | None) -> Path | None:
    for c in ([explicit] if explicit else []) + COOKIES_CANDIDATES:
        if c and c.exists() and c.stat().st_size > 0:
            return c
    return None


def discover(raw: str, llm: LLM, use_llm: bool, log=print) -> Target:
    raw = raw.strip()
    if use_llm:
        d = llm.chat_json(DISCOVER_SYSTEM, raw)
        artist = str(d.get("artist") or "").strip()
        title = str(d.get("title") or "").strip()
        year = _clean_year(str(d.get("year") or ""))
        album = str(d.get("album") or "").strip()
        if not artist or not title:
            raise TuneError(f"LLM could not identify the song "
                            f"(artist={artist!r} title={title!r})")
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
    target = Target(artist, title, year, album)
    target.mb_duration = musicbrainz_duration(artist, title, year, log)
    if target.mb_duration:
        log(f"      duration anchor (MusicBrainz): {target.mb_duration:.0f}s")
    return target


def _clean_year(s: str) -> str:
    m = re.search(r"(1[9]\d{2}|20\d{2})", s)
    return m.group(1) if m else ""


MB_BASE = "https://musicbrainz.org/ws/2"
MB_UA = "tunester/1.0 (music QC pipeline)"


def _mb_get(path: str, log) -> dict | None:
    """One MusicBrainz Web Service call (JSON); None on any failure.
    One retry after a pause on 429/5xx - the API allows ~1 req/s per IP."""
    url = f"{MB_BASE}{path}"
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": MB_UA,
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                time.sleep(3)
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


def download(cand: Candidate, tmp: Path, cookies: Path | None,
             target: Target, log) -> tuple[Path, str] | None:
    """Download a candidate into tmp; return (file path, file name) or
    None."""
    if cand.source == "archive":
        got = _download_http(cand, tmp, log)
        if got is None:
            return None
        return got, Path(urllib.parse.urlparse(cand.url).path).name
    if cand.source == "nzb":
        return _download_nzb(cand, tmp, target, log)
    got = _download_yt(cand, tmp, cookies, log)
    return (got, "") if got else None


def _download_yt(cand: Candidate, tmp: Path, cookies: Path | None, log) -> Path | None:
    for old in tmp.glob("src.*"):
        old.unlink(missing_ok=True)
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


def _download_http(cand: Candidate, tmp: Path, log) -> Path | None:
    for old in tmp.glob("src.*"):
        old.unlink(missing_ok=True)
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
    """Persistent .nzb cache path. A release's NZB is static, so the free
    tier's per-download getnzb quota is spent once per release, not per run."""
    d = Path(os.environ.get("TUNESTER_NZB_CACHE", "/data/.nzb-cache"))
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return d / f"{release_hash}.nzb"


def _nzb_manifest(cand: Candidate, log) -> bytes | None:
    """Fetch a release's NZB manifest; served from the persistent cache
    when available, so the free-tier getnzb is spent once per release."""
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
                bool(target.year) and target.year in c.title)
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
                  log) -> tuple[Path, str] | None:
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
        log("        no audio file in release")
        return None

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


def verify_download(path: Path, cand: Candidate, target: Target,
                    llm: LLM, use_llm: bool, log,
                    file_name: str = "") -> tuple[bool, str, str]:
    """(ok, gate, reason). gate: 'probe' | 'duration' | 'llm' | 'filter-only'."""
    info = probe(path)
    if not info:
        return False, "probe", "no decodable audio stream"
    if info["duration"] < 20:
        return False, "probe", f"too short ({info['duration']:.0f}s) - preview or clip"
    if cand.duration and info["duration"] < 0.9 * float(cand.duration):
        return False, "probe", (
            f"truncated: {info['duration']:.0f}s of {cand.duration:.0f}s "
            "(preview or partial upload)")
    if not cand.duration:
        # Duration anchor: with a known year, only the MusicBrainz
        # year-dated studio length is trusted (a remix-era length must
        # not veto the studio cut); without one, MB then consensus.
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
    if not (use_llm and llm):
        return True, "filter-only", ""
    extra = f'\nsource_file="{file_name}"' if file_name else ""
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
    if not d.get("match"):
        return False, "llm", f"{version}: {d.get('reason', 'rejected')}"
    if version in ("clean", "radio"):
        return False, "llm", f"{version}: {d.get('reason', 'censored')}"
    return True, "llm", ""


def transcode(src: Path, dst: Path, target: Target) -> None:
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-c:a", "libmp3lame", "-b:a", "160k", "-ar", "44100", "-ac", "2",
           "-write_xing", "1",
           "-metadata", f"title={target.title}",
           "-metadata", f"artist={target.artist}"]
    if target.year:
        cmd += ["-metadata", f"date={target.year}"]
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
    if target.year:
        name += f" ({target.year})"
    return sanitize_name(name) + ".mp3"


def tune(raw: str, opts: Options | None = None, llm: LLM | None = None,
         log=print) -> Result:
    """Run the full pipeline. Returns a Result or raises TuneError."""
    opts = opts or Options()
    llm = llm if llm is not None else LLM(log=log)
    cookies = find_cookies(opts.cookies)

    log(f"[1/5] discover: {raw!r}")
    target = discover(raw, llm, opts.use_llm, log)
    log(f"      -> {target.artist} - {target.title}"
        + (f" ({target.year})" if target.year else " (year unknown)"))

    outdir = opts.outdir
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise TuneError(f"cannot create output dir {outdir}: {e}", target=target)
    dst = outdir / target_filename(target)
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
                    got = download(cand, tmp, cookies, target, log)
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
                out_info = probe(dst) or {}
                log(f"[5/5] done -> {dst}")
                return Result(target.artist, target.title, target.year,
                              cand.source, cand.url, dst, gate,
                              out_info.get("duration", 0.0))
        raise TuneError("all sources exhausted - no verified uncensored copy found",
                        target=target)
    finally:
        if not opts.keep_source:
            shutil.rmtree(tmp, ignore_errors=True)
