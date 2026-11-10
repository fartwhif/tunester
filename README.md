# tunester

Dirty song name in, verified uncensored studio MP3 out.

Give it the phrase you half-remember — `"that screaming song about war"`,
`"Beck - Loser (1993)"`, a misspelling, a wrong artist attribution — and it
resolves the track, hunts for the original recording across Usenet,
archive.org and SoundCloud, proves the file it downloaded really is that
recording, and delivers `ARTIST - TITLE (YEAR).mp3` at **160 kbps CBR /
44.1 kHz / stereo**.

Everything outside that spec is rejected: covers, remixes, radio edits, clean
edits, live takes, lyric videos, previews, 12" B-sides mislabeled as the
A-side, and reissues whose tracklist no longer matches the original.

## Pipeline

`pipeline.py` runs five stages; `sources.py` does the searching, `llm.py`
wraps the chat endpoints, `app.py` is the CLI, `web.py` the HTTP API + UI.

1. **discover** — an LLM resolves the rough name to
   `{artist, title, year, cd_year, album}`. An explicitly typed
   `Artist - Title` is *pinned*: the LLM may fix a wrong artist name but may
   not swap the song for a more famous one with a similar title, and if it
   does, the requested values are kept and the year/album are re-derived from
   MusicBrainz. `--no-llm` uses a heuristic parse instead.

2. **search** — sources in priority order, best-to-worst, censored/altered
   uploads filtered out of the file pool:

   | id | source | notes |
   |---|---|---|
   | `nzb` | nzb.life (Usenet) | needs `TUNESTER_NZB_APIKEY` + NNTP creds; auto-skipped when unset |
   | `archive` | archive.org | includes `mediatype:(movies)` — an official video's audio track *is* the studio recording |
   | `soundcloud` | SoundCloud (yt-dlp) | full tracks are DRM-protected, so mostly covers/remixes remain usable |

   Selection is by **per-file** names, never the release title (scene names
   are false fronts), lossless CD rips before lossy, audio before video,
   oldest re-upload before fresh one. Up to `TUNESTER_TRIALS` (5) candidates
   per source.

3. **verify** — the QC gate on each downloaded candidate, in order: ffprobe
   sanity (decodable, ≥20 s, not truncated) → **duration anchor** from
   MusicBrainz (the LLM is never consulted about length: it fabricates one and
   rationalizes the file it is shown) → **AcoustID content gate** (chromaprint
   fingerprint — the only check that catches a file correctly named *and*
   correctly long whose audio is a different song) → **live-version rejection**,
   which does not take the model's word for it: a fingerprint identifying a
   `(Live at …)` take is refused, and so is any reply whose `version` is
   clean/radio/live/cover/remix even when the model also answered `match=true`
   → LLM version gate. Whatever gate rejects a candidate is logged with its
   reason.

4. **transcode** — ffmpeg `-vn -c:a libmp3lame -b:a 160k -ar 44100 -ac 2`
   (video containers ship as MKV/MP4; their audio is extracted).

5. **name** — `ARTIST - TITLE (YEAR).mp3`, then the finished file is
   re-probed against the output spec.

**Tag year**: the `(YYYY)` in the filename, the ID3 `date=` and the API's
`year` field are the **first standard CD release year**, not the studio year
(they differ for pre-CD songs). The studio year still drives every search and
the duration/curation windows.

**Album pluck**: for Usenet targets the `artist + album + year` release search
runs first and up to 10 releases have their *file manifests* checked (cached
NZBs), because the original album is the canonical source; direct search hits
are the fallback. Archive RAR/par2 releases are fetched whole, repaired and
extracted (`unar`, `par2`) before the track is plucked.

## Reuse store

Nothing is fetched, verified or encoded twice. Every file that passes the QC
gate and every finished MP3 is copied into a cache (`TUNESTER_CACHE`, the
container's `tunester-cache` volume — deliberately *not* under the output
dir, so `downloads/` stays nothing but delivered files), keyed two ways:

- **source entries** — source URL + fetched file name. A hit skips the NNTP
  pull or HTTP fetch; the copy is ffprobe-checked and then runs the whole QC
  gate again, so a damaged entry costs a re-download, never a bad delivery.
  A whole-release pluck hits on the plucked track's own cached file instead of
  re-fetching the entire RAR set.
- **delivery entries** — normalized artist + title, the identity the gates
  actually verified. A hit skips the search, the fetch, the gates and the
  transcode: the song was already found and verified once, so the stored copy
  is handed back after one output-spec re-check plus a duration comparison
  with this run's MusicBrainz anchor (`Result.gate`, reported as
  `gate: "cache"` by `--json`). The tag year is *not* in the key — which year
  a pre-CD song first appeared on CD is fuzzy and discovery does not answer it
  the same way twice — and a hit is written out under the stored entry's own
  name (its year included), so a recording the store already served never
  lands a second time under a second year.

`TUNESTER_CACHE_MAX_MB` (30 GB) caps both stores; the least-recently-used
entries are evicted first and a hit counts as use. Nothing about the cache
can fail a tune: an unwritable directory or a corrupt entry degrades to
fetching and verifying normally. `--seed-cache` indexes an existing
`downloads/` into the store so songs already delivered never need fetching.

## CLI

```
python3 app.py "Gorillaz - Feel Up"              # -> ./downloads/…mp3
python3 app.py "that screaming song about war" -o /tmp/out --json
python3 app.py "Beck - Loser" --source nzb --keep-source
python3 app.py "Men Without Hats - Safety Dance" --no-llm
python3 app.py --serve --host 0.0.0.0 --port 8102
python3 app.py --seed-cache            # index ./downloads into the reuse store
```

| flag | meaning |
|---|---|
| `-o, --outdir` | output directory (default `$TUNESTER_OUTDIR` or `./downloads`) |
| `--cookies FILE` | Netscape cookies file for age/explicit access |
| `--source nzb\|archive\|soundcloud` | one source instead of the fallback chain |
| `--keep-source` | do not delete the run's temp working directory, so the downloaded source file (…/`tunester-XXXX`/`src.*`) can be inspected |
| `--no-llm` | skip LLM discovery and the LLM gate: discovery is a plain `Artist - Title` parse (free-form descriptions fail with `TuneError` — without the model there is nothing to resolve them), and verification runs probe/duration/AcoustID only. The version gate is the sole defense against a clean-titled cover/karaoke, so it is off |
| `--no-cache` | ignore the reuse store for this run and do not add its file to it |
| `--seed-cache` | copy the verified MP3s already in the output dir into the reuse store (names that do not parse as `ARTIST - TITLE (YEAR).mp3`, and files failing the output spec, are skipped), then exit |
| `--json` | print the result object (`artist`/`title`/`year`/`source`/`url`/`file`/`gate`/`duration`) |

`TuneError` prints `error: <reason>` on stderr and exits non-zero. The status
ticker (stage progress + every LLM prompt) is written to stdout throughout a
run, so with `--json` the result object is the **last** block on stdout, not the
only one.

## HTTP API + web UI

`python3 app.py --serve` serves a single-page queue UI on `/` plus a JSON API
(no auth — put it behind a proxy if you expose it):

| route | behavior |
|---|---|
| `POST /api/tune` | `{"name": "Artist - Title"}` → `202 {"id": "…"}`; bad/missing name → `400` |
| `GET /api/job/{id}` | the job: `status` (`running`/`done`/`error`/`cancelled`), `log`, and on success `file`/`filename`/`artist`/`title`/`year`; on failure `error` plus the discovered `artist`/`title`/`year` (discovery can succeed with no deliverable file) |
| `GET /api/jobs` | every job, newest first (`id`/`name`/`status`/`filename`/`error`) |
| `GET /api/dl/{id}` | the finished MP3 (`audio/mpeg` attachment); `404` while unfinished, `410` if the file was removed |
| `POST /api/clear` | cancels queued/running jobs and drops the job state (in-flight workers abort at their next log call; downloaded files stay on disk) |
| `GET /api/health` | `{"ok": true}` |

Jobs run in worker threads; the queue is in memory and is cleared by
`/api/clear` or a container restart (the delivered files are on the volume).

Sibling services use this API: GrabbyTube's "Run via tunester" library action
posts `"author - title"`, polls the job, downloads the file, then marks the
track `tuned`. tunester joins the external `shared` docker network so they
reach it as `http://tunester:8102`.

## Configure

Copy `.env.example` to `.env`. Nothing machine-specific is hardcoded in the
source: every knob is read from the environment, and no credential is needed
to start — missing optional settings simply disable their feature.

| variable | default | notes |
|---|---|---|
| `TUNESTER_LLM_PRIMARY_BASE_URL` / `_MODEL` / `_API_KEY` | unset | first-choice OpenAI-compatible chat endpoint; the slot is skipped when the base URL is unset |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY`, `TUNESTER_LLM_MODEL` | unset | second slot, same idea. Endpoints are tried in order per call: a refused/reset/DNS-failed one fails over immediately, a hung non-final one after `TUNESTER_LLM_FAILOVER_WAIT` (90 s) |
| `TUNESTER_LLM_TIMEOUT` / `_LOAD_WAIT` / `_FAILOVER_WAIT` / `_THINK_BUDGET` | `240` / `360` / `90` / `100` | per-request timeout, cold-start wait allowed for the last endpoint, budget for a hung non-final one, thinking-token budget |
| `TUNESTER_ACOUSTID_KEY` | unset | enables the AcoustID content gate (free key at acoustid.org; needs `fpcalc`, shipped in the image). `TUNESTER_ACOUSTID_MIN_CONF` (0.7) is the score at which a confident identification of a *different* recording rejects |
| `TUNESTER_OUTDIR` | `./downloads` | output directory (the container uses `/data`) |
| `TUNESTER_CACHE` | `$XDG_CACHE_HOME/tunester` | reuse store root (`$HOME/.cache/tunester` when XDG is unset); the container uses the `tunester-cache` volume at `/cache` |
| `TUNESTER_CACHE_MAX_MB` | `30720` | cap on the reuse store; LRU entries are evicted first (a hit counts as use) |
| `TUNESTER_WEB_HOST` / `TUNESTER_WEB_PORT` | `0.0.0.0` / `8102` | `--serve` bind |
| `TUNESTER_TRIALS` | `5` | candidates tried per source |
| `TUNESTER_SEARCH_DEPTH` | `15` | results requested per yt-dlp query (SoundCloud; nzb.life/archive.org have their own fixed result windows) |
| `TUNESTER_SEARCH_TIMEOUT`, `TUNESTER_HTTP_TIMEOUT`, `TUNESTER_DOWNLOAD_TIMEOUT` | `120` / `60` / `900` | per-operation budgets (s) |
| `TUNESTER_NZB_MAX_ARCHIVE_MB` | `1024` | cap on fetching a whole RAR/zip release for the archive pluck |
| `TUNESTER_NZB_CACHE` | `/data/.nzb-cache` | downloaded NZB manifests/releases (avoids repeat API pulls per release) |
| `TUNESTER_FFMPEG` / `TUNESTER_FFPROBE` / `TUNESTER_YTDLP` | `ffmpeg` / `ffprobe` / `yt-dlp` | external binaries |
| `WG_*` | see `.env.example` | WireGuard mesh tunnel (same template as the sibling services); unset → the entrypoint skips the tunnel |

Discovery needs **at least one** chat endpoint: with none configured every call
raises `LLMError` ("all LLMs failed"), and at discovery that surfaces as a
traceback. At the *gate* an unreachable LLM is non-fatal — the run logs
`LLM gate unavailable … proceeding filter-only` and the probe/duration/AcoustID
checks stand in for it.

Cookies for age/explicit YouTube material are picked up automatically from
`/shared-cookies/cookies.txt` (the compose mount of the sibling cookie vault),
then `../ytcookies/data/cookies.txt`, then `data/cookies.txt`, or `--cookies`.

## Run

Docker (the shipped path — image carries `ffmpeg`, `yt-dlp`, `fpcalc`,
`par2`, `unar`, `p7zip`, plus the WireGuard tools):

```
docker network create shared   # external bridge shared with the sibling services
docker compose up -d --build
docker logs tunester           # WireGuard handshake + "tunester listening on…"
curl -s localhost:8102/api/health
curl -s -X POST localhost:8102/api/tune -d '{"name":"Peter Gabriel - Sledgehammer"}'
curl -s localhost:8102/api/jobs
```

`./downloads` is mounted at `/data` (MP3 output), the named volume
`tunester-cache` at `/cache` (reuse store), `../ytcookies/data` at
`/shared-cookies`, and `.env` read-only at `/app/.env`. The NZB cache stays
under `/data/.nzb-cache`.

The container needs `NET_ADMIN` + `net.ipv4.conf.all.src_valid_mark=1` for the
tunnel; the entrypoint also pins the docker bridge subnet onto `eth0` in table
`51820` so the full-tunnel route table does not swallow replies from sibling
containers.

Bare host: any Python with `nntplib` — the image uses **3.12**; the module was
removed in 3.14 (PEP 594), and the pipeline imports it lazily inside the NNTP
fetch, so a newer interpreter merely logs `NNTP connect failed: No module named
'nntplib'` and gets nothing from Usenet instead of crashing. `ffmpeg`, `ffprobe`
and `yt-dlp` must be on `PATH`; there are no third-party Python dependencies.

```
python3 -m venv .venv && .venv/bin/python app.py --serve
```

## Status ticker

Every LLM call appends its full system + user prompt to the job log (single
choke point in `llm.py`), so `GET /api/job/{id}`'s `log` and `docker logs`
show exactly what was asked of the model and which gate each candidate died
on — which is how the curation rules in `AGENTS.md` were derived.
