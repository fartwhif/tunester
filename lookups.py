r"""Third store: the answers, not the audio.

`pipeline`'s cache holds the big things - fetched source bytes and finished
MP3s. Everything else the tuner spends time on is a small, repeatable
ANSWER: a MusicBrainz recording query, an AcoustID fingerprint lookup, an
archive.org item's file list, an nzb.life search page, a yt-dlp search
listing, an LLM verdict on a fixed prompt, a release's tracklist sidecar
read off NNTP. Those were re-fetched on every run: one tune chains an
album + cd_year + duration lookup (plus a recording query per candidate and
an AcoustID lookup per download), so re-tuning a song a GrabbyTube batch
already asked about paid the API latency and the rate-limit budget again
for identical bytes.

Same directory, same `<sha1>.bin` + `<sha1>.json` layout, same
blob-then-sidecar `os.replace` publish, and shared LRU eviction with the
media stores (`pipeline._cache_evict` walks every `*.bin` under the cache
root), so the answers count against `TUNESTER_CACHE_MAX_MB` and are dropped
oldest-first along with the audio. One difference on purpose: NO 1 KB floor.
An API answer is often a few hundred bytes (`{"results":[]}`), and an empty
result list IS a verdict worth keeping.

Subtrees:
  lookups/    HTTP API responses; `kind` in the sidecar says which service.
  nzb-text/   NNTP tracklist/nfo text: Usenet articles are immutable, so
              these carry no expiry at all.
  llm/        model replies (the discovery pin, the QC version gate); the
              payload is the reply TEXT, not the HTTP envelope.

Expiry is per entry (the sidecar records the TTL that was in force when it
was written) and comes from the environment at call time:
  TUNESTER_LOOKUP_TTL_HOURS    reference answers - MusicBrainz, AcoustID,
                               archive.org item metadata (default 72; 0
                               disables the store)
  TUNESTER_SEARCH_TTL_MINUTES  search listings - nzb.life, archive.org
                               advancedsearch, yt-dlp (default 60; 0
                               disables)
  TUNESTER_LLM_TTL_HOURS       model verdicts (default 168 = a week; 0
                               disables) - a re-tune of the same candidate
                               asks byte-identical questions, and a model
                               swap moves to a different key anyway
An entry written under a different TTL than the one in force now is stale:
changing a knob refreshes the store instead of serving answers the new
setting says are too old.

Only a SUCCESS is ever published. A timeout, a 429, an AcoustID `queued`
or a bad key is the service NOT answering, and freezing that would let one
bad moment decide a later run's verdicts. `enabled_scope(False)` (what
`--no-cache` enters) disables reads and writes, exactly like the media
stores.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import time
from contextvars import ContextVar
from pathlib import Path

# Subtree names (all live under the one cache root).
API_STORE = "lookups"
TEXT_STORE = "nzb-text"
LLM_STORE = "llm"

_ENABLED: ContextVar[bool] = ContextVar("tunester_lookups_enabled",
                                        default=True)


@contextlib.contextmanager
def enabled_scope(on: bool):
    """Context manager turning the whole answer store off for a run.

    `pipeline.tune` enters it with `Options.use_cache`, so `--no-cache` skips
    these fetches' cache too - the same read-AND-write switch the media
    stores honor. Context-local, not a module global: the hub's web server
    runs tunes in threads, and a clear-queue run must not switch off another
    job's caching."""
    token = _ENABLED.set(on)
    try:
        yield
    finally:
        _ENABLED.reset(token)


def cache_root() -> Path:
    """The cache root: exactly `pipeline.CACHE_DEFAULT`, recomputed per call
    so a run that relocates the store (`TUNESTER_CACHE`, or a test pointing
    the tuner at a tmpdir) moves the answers with the audio. `pipeline` keeps
    its own constant for the media stores; a test pins the two equal."""
    return (Path(os.environ.get("TUNESTER_CACHE")
                 or Path(os.environ.get("XDG_CACHE_HOME")
                         or Path.home() / ".cache") / "tunester"))


def lookup_ttl(kind: str) -> float | None:
    """Seconds an answer of `kind` stays good, or None for "no expiry".

    0 means the store is OFF for that kind: neither read nor written, so
    `TUNESTER_LOOKUP_TTL_HOURS=0` is the kill switch for anyone who needs the
    API calls live (a service whose answers must be current, a debugging
    run); `TUNESTER_SEARCH_TTL_MINUTES=0` does the same for the listing
    kinds. `text` has no expiry of its own but follows the reference switch.
    """
    if not _ENABLED.get():
        return 0.0
    if kind == "search":
        return 60.0 * int(os.environ.get("TUNESTER_SEARCH_TTL_MINUTES", "60"))
    if kind == "llm":
        return 3600.0 * int(os.environ.get("TUNESTER_LLM_TTL_HOURS", "168"))
    hours = int(os.environ.get("TUNESTER_LOOKUP_TTL_HOURS", "72"))
    if hours == 0:
        return 0.0
    return None if kind == "text" else 3600.0 * hours


def key(*parts: object) -> str:
    """Store key of one answer: sha1 over its identity parts.

    Every part must be part of the ANSWER's identity, not of the call: the
    request URL, the fingerprint + duration, the model id + full prompt.
    Joined on \\x1f so `("ab","c")` and `("a","bc")` differ. Parts are
    stringified rather than joined raw: a caller that passes the number it
    asked with (`depth`, a duration) is naming the answer, and
    `"\x1f".join` raising `TypeError` inside the cache would take the whole
    search down - a wrapper must never be the reason a source finds nothing.
    `str()` of a str is itself, so the keys of every existing entry are
    unchanged."""
    return hashlib.sha1(
        "\x1f".join(p if isinstance(p, str) else str(p)
                    for p in parts).encode("utf-8")).hexdigest()

def get(sub: str, entry: str, ttl: float | None) -> bytes | None:
    """Payload of a live cached answer, or None on miss/expiry/disabled.

    A DEAD entry is UNLINKED, not just ignored: `put` never overwrites a
    published blob, so a record that cannot be honored - expired, written
    under another TTL, or whose blob/sidecar pair is torn (a run killed
    between the two publishes) - would otherwise block its own refresh
    forever, silently, for the rest of the store's life. A cache problem
    must never fail a tune; it may only cost the fetch it was to save."""
    if ttl == 0 or not _ENABLED.get():
        return None  # store off: never read, never delete
    d = cache_root() / sub
    blob, side = d / f"{entry}.bin", d / f"{entry}.json"
    try:
        payload = blob.read_bytes()
        meta = json.loads(side.read_text())
    except OSError:
        _drop(d, entry)  # missing or torn pair: let it be fetched again
        return None
    except ValueError:
        _drop(d, entry)  # sidecar is not JSON any more
        return None
    if not isinstance(meta, dict):
        _drop(d, entry)
        return None
    if meta.get("ttl") != (None if ttl is None else ttl):
        _drop(d, entry)  # written under another TTL: refresh
        return None
    if ttl is not None:
        try:
            saved = float(meta.get("saved", 0))
        except (TypeError, ValueError):
            saved = 0.0
        if time.time() - saved > ttl:
            _drop(d, entry)
            return None
    try:  # a hit refreshes the entry: LRU keeps what is used
        os.utime(blob, None)
        os.utime(side, None)
    except OSError:
        pass
    return payload


def put(sub: str, entry: str, kind: str, source: str, payload: bytes,
        ttl: float | None, log=None) -> bool:
    """Publish one successful answer; False when nothing was written.

    `source` records WHERE the answer came from (the request URL, the release
    the text was read from) so the store can be inspected and an entry's
    provenance checked. Blob then sidecar, both through `os.replace`, so no
    reader ever sees a partial answer."""
    if ttl == 0 or not _ENABLED.get():
        return False  # store off: never write
    d = cache_root() / sub
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if log:
            log(f"        lookup cache dir unavailable: {e}")
        return False
    blob, side = d / f"{entry}.bin", d / f"{entry}.json"
    try:
        if blob.stat().st_size:
            return False  # already published
    except OSError:
        pass
    tmp_blob = tmp_side = None
    try:
        fd, tmp_blob = tempfile.mkstemp(dir=str(d), prefix=entry + ".",
                                        suffix=".part")
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.chmod(tmp_blob, 0o644)
        fd, tmp_side = tempfile.mkstemp(dir=str(d), prefix=entry + ".",
                                        suffix=".json.part")
        with os.fdopen(fd, "w") as f:
            json.dump({"kind": kind, "source": source,
                       "ttl": None if ttl is None else ttl,
                       "saved": time.time(), "bytes": len(payload)}, f)
        os.replace(tmp_blob, blob)
        tmp_blob = None
        os.replace(tmp_side, side)
        tmp_side = None
    except OSError as e:
        if log:
            log(f"        lookup cache write failed: {e}")
        for stray in (tmp_blob, tmp_side):
            if stray:
                try:
                    os.unlink(stray)
                except OSError:
                    pass
        return False
    return True


def _drop(d: Path, entry: str) -> None:
    for p in (d / f"{entry}.bin", d / f"{entry}.json"):
        try:
            p.unlink()
        except OSError:
            pass


def drop(sub: str, entry: str) -> None:
    """Forget one cached answer so the next call re-fetches it."""
    _drop(cache_root() / sub, entry)


def drop_key(sub: str, key_parts: tuple[str, ...]) -> None:
    """`drop` by the same `key_parts` a `cached` call passed."""
    _drop(cache_root() / sub, key(*key_parts))


def cached(kind: str, key_parts: tuple[str, ...], fetch, log,
           source: str = "", sub: str = API_STORE) -> tuple[bool, bytes | None]:
    """(hit, payload) of one idempotent API answer.

    `fetch()` runs ONLY on a miss and returns the response bytes; a None
    (the call failed) publishes nothing. This is the entry point the service
    wrappers use, so the TTL rules, the publish rules and the hit log line
    exist once."""
    ttl = lookup_ttl(kind)
    entry = key(*key_parts)
    got = get(sub, entry, ttl)
    if got is not None:
        log(f"        lookup cache hit ({kind})")
        return True, got
    payload = fetch()
    if payload is None:
        return False, None
    put(sub, entry, kind, source, payload, ttl, log)
    return False, payload
