# tunester — project notes

Docker pipeline: dirty song name → verified uncensored 160 kbps CBR MP3 in
`downloads/` (container `/data`). CLI: `app.py "<query>"`; `pipeline.py`
orchestrates, `sources.py` searches, `llm.py` wraps the llm1 chat API.

## Sources (priority order)
1. **nzb.life / Usenet** (top priority). Free tier: `getnzb` 429
   "Already Downloaded" = 24 h per-release cooldown; NZB cache
   `/data/.nzb-cache/<hash>.nzb` makes re-fetch free. Every getnzb on an
   uncached release spends one free-tier download — keep spends minimal.
   NNTP: `news.usenetserver.com:563` (creds in `.env`), yEnc with per-part
   CRC-32 gate in `pipeline._yenc_part`.
2. SoundCloud (yt-dlp; full tracks DRM-protected → search works, download
   mostly errors out).
3. archive.org (file `length` is parsed into `Candidate.duration`).
## API / interop
- `POST /api/tune` job objects carry `artist`/`title`/`year` (the
  discovered fixup data) on BOTH `done` and `error` statuses. `TuneError`
  has a `.target` (Target|None): discovery succeeds even when no verified
  file is delivered, so a failed job still exposes artist/title/year.
- tunester is attached to the external `shared` docker network so other
  services reach it by container name. GrabbyTube's "Run via tunester"
  library feature (select tracks -> `POST /api/tune` with "author - title",
  poll `GET /api/job/{id}`, `GET /api/dl/{id}`) consumes this at
  `http://tunester:8102` (env `GRABBY_TUNESTER_URL`).
- On a file replacement grabbytube sets `tuned: true` on the track
  (catalog + per-item JSON, hence `grabby-index.json` for DrivePod); the
  library UI shows it as a read-only badge. Fixup-only runs do NOT set it.

## QC gates (`verify_download`)
probe (ffprobe: decodable, ≥20 s, not truncated) → **duration anchor** →
**AcoustID content gate** (optional) → LLM version gate (clean/radio →
reject).

- **AcoustID content gate** (`_acoustid_identify`, needs
  `TUNESTER_ACOUSTID_KEY`; fpcalc = `libchromaprint-tools`): fingerprints the
  download (chromaprint) and looks it up on AcoustID. A confident
  identification (best result score ≥ `TUNESTER_ACOUSTID_MIN_CONF`, default
  70) of a DIFFERENT artist/track rejects with gate `acoustid`; a match is a
  strong pass. This is the ONLY gate that catches a mislabeled article — a
  file named/tagged "Loser" that contains a different song of the right
  length passes every name/duration/LLM gate (see the Beck "Loser" 2026-09-28
  pitfall below). Silently skipped when the key or fpcalc is absent.
- The LLM is NOT trusted with duration: pretests showed it fabricates a
  matching studio length and rationalizes the input (said "matches ~2:56"
  for a 175 s file and "~3:38" for a 231 s file of the same song).
- **Duration anchor = MusicBrainz** (`musicbrainz_duration`, computed in
  `discover`): recording search, exact clean title, artist-credit
  containment, length >30 s, median of the largest ≥2 within-15 s
  cluster. **With a known target year only year-dated clusters are
  trusted** (recordings dated ±1 then ±3 years of it); no such cluster →
  gate SKIPPED. The global clean cluster can be remix-era (HAA: 319 s
  vs the 2001 studio cut), so trusting it vetoes the right file.
  Year-less targets fall back to the global MB cluster, then the legacy
  multi-source consensus (`_consensus_duration`).
- The gate sees the real scene file name (`source_file`) and a
  `source=video release` hint; for multi-track releases the prompt judges
  by `source_file` (a different track → mismatch). Video releases are
  judged on their audio track only.

## Selection and curation filters
Release names are untrustworthy — selection is by **per-file** names:
- `_nzb_file_name` parses part subjects: quoted filename first
  (`- "01-…-you.mp3" yEnc`), else the longest **balanced** bracket tag
  ending in an extension (nested `[a1-beck-loser_[lp_version].flac]`),
  else the last token. WtFnZb's empty `""` quotes must NOT match.
- File pool: per-file `make_bad_filter` rejects remix/censored files,
  then **a title-hitting file always beats a non-hitting one** — 12"
  maxis are named after their A-side (`Beck-Loser-VLS…` ships
  `b2-beck-fume.flac` ID3-tagged "Loser").
- `_year_mismatch`: with a known target year, releases whose title
  carries a year more than ±5 away are dropped at pick (2014 HAA remix
  pack vs a 2001 target; 2005 single pressings of the same studio cut
  pass); year-less titles always pass.
- `TRIALS_PER_SOURCE` default 5 (env `TUNESTER_TRIALS`).
- **Age preference**: nzb candidates carry the release post timestamp
  (`Candidate.posted`, from the feed `<pubDate>`); `pick` sorts
  `(score, popularity, -posted)` so, all else equal, OLD established releases
  outrank fresh uploads - new uploads' names/tags have proven unreliable
  (2015/2025/2026 "Mellow Gold" re-uploads vs the 2011-2013 rips). The
  album-pluck `_rank` uses the same oldest-first tiebreak.
- `search_candidates(..., year=, album=)`: nzb query appends the year
  (free-text relevance hint; pushes original pressings to the top of
  nzb.life results). nzb.life ANDs free text, so if no release title
  carries the year the yeared query returns 0 - `_nzb_search` then
  retries year-less (searches are free); archive tries the title query,
  then the album query.

## Album pluck (nzb)
`discover` returns the original album and the MusicBrainz duration
anchor. The `artist + album + year` nzb search runs for every nzb target:
candidates are ranked (album in title, year in title, then oldest first)
and up to 10 are
NZB-checked (cache-first `_nzb_manifest`); the first release whose file
list contains a clean file with the song title becomes the pluck
candidate and is **tried before** direct search hits — the original
album is the canonical source. Direct hits remain the fallback.
Early-return on the first verified release caps quota spend.

## Usenet curation pitfalls (names are false fronts)
- 12" maxis are named after the A-side: `Beck-Loser-VLS-FLAC-1994-ERP`
  contains `b1-beck-alcohol.flac` and `b2-beck-fume.flac` with ID3s
  tagged "Loser" — the pipeline once delivered Fume for "Loser".
- Nested-bracket filenames (`[a1-beck-loser_[lp_version].flac]`) broke
  the old non-nested regex: a1 vanished from the pool, so the largest
  file (fume, 97 MB) won.
- Posting formats vary: quoted filenames, empty `""` quotes (WtFnZb),
  bare last token — `_nzb_file_name` handles all.
- Reissues silently change tracklists AND ride false title fronts: the
  2014 Discovery reissue (14 tracks) removed Human After All, Open Your
  Arms, VIP, On The Rocks and Trackback - and on nzb.life its rips are
  titled "(2001)". ALL 10 top "Daft Punk Discovery 2001" results were
  2014 content, so the pluck must check past the top ranks (cap 10) and
  judge manifests, never titles. Countermeasures: per-file manifest
  checks, `_year_mismatch`, per-file selection.
- **Beck "Loser" (2026-09-28): names, ID3, AND duration ALL lie.** A file
  `a1-beck-loser_(lp_version).flac` (ID3 "Loser", 236.7 s = the MusicBrainz
  anchor) was a completely different song. Normalized FFT cross-correlation
  showed the VLS a1, the archive.org "beck-loser" upload, and the Mellow
  Gold rips' "01 - Loser" were THREE distinct songs (pairwise ~0.02). On
  nzb.life EVERY "Beck Mellow Gold" release (2011 EOS, 2013 WRS, 2013
  FORSAKEN carrying the real DGCD-24634 catalog number, 2015, 2025) is the
  SAME mislabeled early-Beck demo compilation (Loser + "Pay No Mind
  (Snoozer)" + "Whiskeyclone (Hotel City 1997)" + …); the genuine Mellow
  Gold album (Loser, Sexx Laws, New Pollution, …) is NOT in its catalog. Its
  track 1 "Loser" (235.0 s, par2-verified FORSAKEN CD-FLAC) is the
  canonical one - identical (0.983) across the 2011/2013/2015 re-uploads.
  The official YT video (YgSPaXgAdzE) is bot-blocked from this IP even with
  the shared cookies (player formats withheld), so it is not a usable
  reference/source. When a source's whole catalog is one mislabeled disc,
  only content verification (AcoustID) + oldest-first ranking help.
- **LLM discover is nondeterministic about `album`** (2026-09-28): for
  "Beck - Loser (1993)" it sometimes returns album="" - then the album
  pluck silently never runs and the fallback direct search plucks the VLS
  12" a1 (the different song). `discover` now backfills an empty album via
  `musicbrainz_album`: the MB search API is itself nondeterministic and
  has no release-group types, so it tallies release titles across every
  exact-title recording and picks the MOST-PRESSED title (the original
  album has worldwide reissues; one-off compilations/promos appear once),
  excluding titles equal to the song (the single = the 12" false front),
  preferring a +/-1-year window when the year is known. Beck -> Mellow
  Gold; Sledgehammer/Big Time -> So; One Step Closer -> Hybrid Theory.

- All LLM prompts (system + user) are appended to the status ticker via
  `LLM(log=...)` inside `chat()` — single choke point; both the discover
  and gate prompts pass through it.

## Video fallback (nzb)
Audio files always beat video files within a release; video containers
(mkv/mp4/…) are fetched and transcoded audio-only (`-vn`), then QC'd.

## Archive (RAR/zip) releases (nzb)
When a release has NO bare audio/video file, `_nzb_archive_pluck` fetches the
WHOLE set over NNTP (cap `TUNESTER_NZB_MAX_ARCHIVE_MB`, default 1024 MB),
verifies/repairs with `par2` when a `.par2` is present, extracts with `unar`
(primary volume: `.part1.rar` > plain `.rar` > first), then plucks the track
from the extracted tree with the same title-hit/format/size ranking as the
bare-file path. Image ships `unar`, `p7zip-full`, `par2`,
`libchromaprint-tools`. Scene RAR volume naming varies (FORSAKEN's rip = one
`.rar` + `.r00…` siblings), so ALL files are fetched, not just `*.rar` —
`unar` resolves sibling volumes from the working directory.

## Deploy (any code change)
```
python3 -m py_compile pipeline.py sources.py llm.py app.py
docker compose build -q   # "buildx isn't installed" warning = ignore
docker compose up -d --force-recreate
```
`.env` is host-mounted `:ro` (live); the web service only sees env at
entrypoint. `docker exec` does NOT inherit entrypoint env:
`docker exec tunester bash -c 'set -a; . /app/.env; set +a; cd /data;
python3 /app/app.py "<query>"'`. Container = Python 3.12 (has `nntplib`),
host = 3.14 (no `nntplib`) — keep `import nntplib` lazy in-function.
bash tool: no `name` (service mode) for long runs — `async: true` +
timeout; results auto-deliver, never poll.

## LLM (prioritized failover list)
`llm.py` tries backends in priority order per call; first valid
response wins. Order (edit `_llm_endpoints()`): 1) **qwen38-27b**
(llama.cpp edge `qwen38-27b.llmhost.example.com/v1`, model
`/models/Qwen3.8-27B-UD-Q6_K_XL.gguf`, ~2-5 s/call) 2) **llm1**
(env `OPENAI_*`; healthy ~9 s, observed 56-379 s under load 2026-09-27).
Non-final backend budget `FAILOVER_BUDGET`=90 s (hung endpoint); a dead
endpoint (refused/DNS/reset, `_DEAD_REASONS`) fails over immediately;
the last fallback keeps `LOAD_WAIT`=360 s cold-start wait.
`CHAT_TIMEOUT`=240 s. All keys share one bearer (qwen reuses
`OPENAI_API_KEY`). Gate degrades to filter-only if ALL backends fail.

## Verified deliveries (2026-09-27)
- Peter Gabriel – Sledgehammer (1986): 282.5 s, 160 k CBR — good.
- Linkin Park – One Step Closer (2000): 155.7 s (2:36 studio per
  MusicBrainz 153.2 s) — album FLAC plucked from Hybrid Theory CD-FLAC
  after the DDC-1080p video (175 s alternate edit) was rejected on
  duration.
- Peter Gabriel – Big Time (1986): 268.0 s (4:28 So album version) —
  album pluck from So (WALKMAN 16-bit FLAC), 0 direct search hits.
- Daft Punk – Human After All (2001): 319 s (5:19 studio; confirmed by
  the release NFO and the MusicBrainz canonical ~319 s cluster). All 10
  top "Discovery 2001" nzb results were 2014-reissue false fronts
  (no HAA), so album pluck failed and the pipeline fell back to the
  direct search, plucking the standard version from the 2005 CD.
  (Earlier guess that the studio cut was ~3:30/210 s was WRONG.)
- Beck – Loser (1993): 235.1 s, 160 k CBR (2026-09-28) — track 1 "Loser"
  of the early-Beck compilation that EVERY nzb.life "Mellow Gold" release
  actually is; par2-verified FORSAKEN CD-FLAC rip (real DGCD-24634 catalog
  no.), cross-correlation-verified (0.983) identical across the 2011 EOS /
  2013 WRS / 2015 re-uploads. Replaces two earlier WRONG deliveries (269.7 s
  "Fume" mis-tagged B-side; then the 236.7 s VLS a1 — a different song that
  passed every name/duration/LLM gate). See the 2026-09-28 curation pitfall.
