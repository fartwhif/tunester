# tunester — project notes

Docker pipeline: dirty song name → verified uncensored 160 kbps CBR MP3 in
`downloads/` (container `/data`). CLI: `app.py "<query>"`; `pipeline.py`
orchestrates, `sources.py` searches, `llm.py` wraps the LLM chat APIs.

## Reuse store (2026-11-10)
`TUNESTER_CACHE` (container: named volume `tunester-cache` at `/cache` —
**not** under `/data`, so `downloads/` stays purely the user's delivered
files) holds two subtrees, each `<sha1>.bin` + a `<sha1>.json` sidecar,
written blob-then-sidecar via `os.replace` so no reader ever sees a partial
entry (`_cache_write`):
- `sources/` — key `sha1(source URL, fetched file name)` (URL alone for
  archive.org/SoundCloud, where `name=""`). Skips the fetch; the hydrated
  copy is ffprobe-checked and then runs the FULL gate + transcode again, so
  the cache never changes what gets delivered. `_cache_release_hit` covers
  whole-RAR plucks: it scans sidecars for this release URL + a name
  containing the target title (the same file the pluck would have picked),
  so a different track from the same release is a miss.
- `deliveries/` — key `sha1(_norm(artist), _norm(title))`, i.e. the song
  identity the gates verified. **The tag year is NOT in the key**: which year
  a pre-CD song first hit CD is fuzzy and `discover` does not answer it the
  same way twice (observed: "Nena - 99 Luftballons" → 1983, 1984 and 1986
  inside an hour, landing three copies in `downloads/`), so a year-keyed
  store re-downloaded the same recording on nearly every repeat. The year is
  in the sidecar and drives the file name: on a hit the **stored** name is
  used (`_delivery_name`), so file name and ID3 date always agree. Hit =
  search/fetch/gates/transcode all skipped; the copy is verified by
  `check_output` plus a ±15 s comparison against this run's `mb_duration`
  anchor before it lands (a same-title entry that is a different cut is a
  miss, not a substitution; `Result.gate == "cache"`). A spec failure or a
  ruled-out cut drops the entry so the fresh delivery replaces it; a bad
  *copy* of a sound blob is kept. Seeded from `downloads/` by
  `app.py --seed-cache` (parses `ARTIST - TITLE (YEAR).mp3` names, runs
  `check_output` on each, counts stored / skipped / already-in-store).
  Known limit of the exact-title key: a parenthetical **subtitle** the LLM
  includes or drops run to run ("I Wanna Dance with Somebody" vs "(Who Loves
  Me)", 2026-11-10) is a separate entry and a second file. Dropping paren
  groups from the key is NOT safe — "(Live at …)", "(Reprise)" and a genuine
  B-side live there.
`--no-cache` = `Options.use_cache=False` skips reads AND writes in both
stores. `_cache_evict` caps both subtrees at `TUNESTER_CACHE_MAX_MB`
(default 30720) by entry mtime; a hit refreshes mtime so used entries
survive. Every cache failure (unwritable dir, corrupt blob/sidecar, copy
error) logs and returns a miss — a cache problem NEVER fails a tune.

## Sources (priority order)
1. **nzb.life / Usenet** (top priority). Account is **VIP membership**
   (363 days left as of 2026-09-29): no free-tier getnzb quota to conserve. `getnzb`
   429 "Already Downloaded" (24 h per-release cooldown) is raised, not
   retried (`sources._nzb_get`); NZB cache `/data/.nzb-cache/<hash>.nzb`
   still avoids repeat getnzb per release.
   NNTP: `news.usenetserver.com:563` (creds in `.env`), yEnc with per-part
   CRC-32 gate in `pipeline._yenc_part`.
2. archive.org (file `length` is parsed into `Candidate.duration`;
   `mediatype:(movies)` items and video containers are also searched -
   an official music video's audio track IS the studio recording and is
   transcoded `-vn` via the same video-release path as nzb).
3. SoundCloud (yt-dlp; full tracks DRM-protected → search works,
   download mostly errors out; the uploadable remainder is mostly
   covers/remixes/clean-titled karaoke - moved after archive 2026-10-21).
## API / interop
- `POST /api/tune` job objects carry `artist`/`title`/`year` (the
  discovered fixup data) on BOTH `done` and `error` statuses. The
  `year` field is the TAG year (first standard CD release year,
  studio year when that is unknown - see "Tag year" below). `TuneError`
  has a `.target` (Target|None): discovery succeeds even when no verified
  file is delivered, so a failed job still exposes artist/title/year.
- `GET /api/jobs` lists every job (newest first; `id`/`name`/`status`/
  `filename`/`error`); `POST /api/clear` cancels all jobs and drops the
  job state (in-flight workers abort at their next `log()` call -
  bounded by the current LLM/download step - and downloaded files are
  left in place). The web UI shows the queue list + a clear button.
- tunester is attached to the external `shared` docker network so other
  services reach it by container name. GrabbyTube's "Run via tunester"
  library feature (select tracks -> `POST /api/tune` with "author - title",
  poll `GET /api/job/{id}`, `GET /api/dl/{id}`) consumes this at
  `http://tunester:8102` (env `GRABBY_TUNESTER_URL`).
- On a file replacement grabbytube sets `tuned: true` on the track
  (catalog + per-item JSON, hence `grabby-index.json` for DrivePod); the
  library UI shows it as a read-only badge. Fixup-only runs do NOT set it.

## Tag year (first standard CD)
Delivered files are tagged with the **first standard CD release year**,
not the studio recording year: `Target.year` = original studio year
(still used for ALL search/curation: nzb query, `_year_mismatch`, MB
duration/album windows); `Target.cd_year` = tag year; the filename
`(YYYY)`, ID3 `date=`, and `Result.year` (hence the API job field) all
use `cd_year or year`.
Chain in `discover`: LLM `cd_year` (DISCOVER_SYSTEM field; accepted only
when 4-digit and >= the studio year - a CD cannot predate the recording)
-> `musicbrainz_cd_year` (one recording search with `inc=releases`;
earliest dated CD-medium release, official status preferred, bootlegs
skipped) -> studio year.
- 2026-10-21 (MWH "The Safety Dance"): qwen38-27b answers `""` for
  cd_year in the 4-field JSON and hallucinates **1985** when asked a
  targeted follow-up question - so NO targeted follow-up call was added.
  MB's only pre-2010 CD record is a **1983** CA pressing with no label
  info, so the delivery is tagged 1983. Wikipedia says 1984 (Statik
  European CD with bonus tracks); Discogs is bot-blocked from this
  host, so the pre-CD tag year for this song is machine-ambiguous.
- MB early-CD coverage is patchy (label-less stubs, missing pressings);
  mid-80s-onward songs normally get cd_year == studio year, and pre-CD
  songs are where the uncertainty lives.

## Artist & title pin (2026-11-03)
`discover` pins the artist AND the title the user explicitly typed. The
LLM is licensed to fix "wrong artist names" / vague descriptions and can:
(1) re-attribute a song to a DIFFERENT artist with the same title ("Ace
of Base - Cruel Summer" -> "Roy Orbison - Cruel Summer (1964)"), or
(2) substitute a more famous SAME-artist song with a similar title
("Gorillaz - Feel Up" -> "Gorillaz - Feel Good Inc. (2005)"). Every
downstream gate then targets the wrong song consistently and passes it
(AcoustID `_acoustid_matches` matches against `target.artist`/`title`,
which are now the wrong ones).
`_requested_artist` / `_requested_title` parse an explicit "Artist -
Title" from the raw input (same regex as the --no-llm heuristic;
free-form descriptions return "" - there the LLM has the wheel). The
requested title is cleaned first: a repeated artist prefix ("Gorillaz -
Gorillaz - Feel Up (HD)" -> title "Feel Up"), a trailing year, and
technical tags (`_TITLE_TAGS`: HD/320kbps/Official Music Video/...).
`_artist_matches` fuzzy-compares with normalized equality / containment /
SequenceMatcher >= 0.8 (misspellings, casing, punctuation, leading
articles, name changes like "Kanye West" ~ "Ye" are the SAME; "Roy
Orbison" !~ "Ace of Base"). `_title_matches` is stricter - NO containment
(song titles share words: "Summer" in "Cruel Summer" must not pass) -
normalized equality or SequenceMatcher >= 0.8 ("Feel Goud Inc" ~
"Feel Good Inc.", "Feel Up" !~ "Feel Good Inc."). A mismatch pins the
requested value and drops the LLM's year/cd_year/album (they describe the
substituted song); the MB lookups re-derive them. Outcome for an unknown
track: an honest "all sources exhausted" instead of a different song's
file. The DISCOVER_SYSTEM prompt also forbids both substitutions (soft);
the pins are the hard guarantee (LLM output is nondeterministic).

## QC gates (`verify_download`)
probe (ffprobe: decodable, ≥20 s, not truncated) → **duration anchor** →
**AcoustID content gate** (optional) → **LIVE-version gate** → LLM version
gate (any non-studio version → reject).

- **AcoustID content gate** (`_acoustid_identify`, needs
  `TUNESTER_ACOUSTID_KEY`; fpcalc = `libchromaprint-tools`): fingerprints the
  download (chromaprint) and looks it up on AcoustID via the **v2 API**
  (`POST https://api.acoustid.org/v2/lookup`, param `client`, `meta=recordings`;
  the legacy `acoustid.org/api/1/` endpoint is gone - 404). Confidence is the
  the result `score` on a **0..1** scale. The best result is a cluster of
  similar recordings: if ANY member matches the target it is a strong
  pass, else the cluster's top recording that is NOT the target rejects
  with gate `acoustid` when score ≥ `TUNESTER_ACOUSTID_MIN_CONF` (default
  **0.7**), a match is a strong pass. This is the ONLY gate that catches a
  mislabeled article — a file named/tagged "Loser" that contains a different
  song of the right length passes every name/duration/LLM gate (see the Beck
  "Loser" 2026-09-28 pitfall below). Silently skipped when the key or fpcalc
  is absent. Key = the user's API key from acoustid.org (set in `.env`).
- **LIVE-version gate (2026-11-03)**: a live take of the correct song by
  the correct artist at the correct length must NEVER be delivered - the
  deliverable is the original uncensored studio recording as it appears on
  the standard commercial CD. Two hard (non-LLM) checks:
  (1) AcoustID - if the fingerprint identifies the content as a LIVE version
  (`\blive\b` in the identified title, e.g. "Bohemian Rhapsody (Live at
  Wembley)") it is rejected with gate `acoustid`, even though it would pass
  `_acoustid_matches` (target title is a substring of the live title). A
  target that IS itself a live recording is exempt (rare). Word-boundary
  match, so "Livin on a Prayer" is not flagged.
  (2) LLM version vocabulary - the LLM's `match` flag is NOT trusted on its
  own: `version` in {clean, radio, live, cover, remix} rejects regardless of
  `match` (the LLM was observed answering match=true while labeling the
  version live). `VERIFY_SYSTEM` also states a live performance is never
  acceptable. Name-level live is already dropped by NEVER_WORDS ("live")
  except when it is part of the artist name (band "Live").
- **Karaoke/instrumental blind spot** (2026-10-21): AcoustID returns
  `None` for instrumentals (they are not in the recording DB), so a
  clean-titled karaoke upload of the RIGHT song at the RIGHT length
  passes every gate - no text-only gate can see that vocals are
  missing (Men Without Hats "The Safety Dance": the nzb release was an
  expired "Extended Club Mix" and SoundCloud's only clean-titled
  candidate was a troll's karaoke; MB anchor is 164 s = the genuine
  2:45 studio cut, which the 2:42 karaoke matched). Mitigation = source
  priority: archive.org (official videos carry the studio audio) now
  outranks SoundCloud, and `_archive_search` merges a
  `mediatype:(movies)` query + video container extensions.
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
- `NEVER_PHRASES` also drops dance/remix labels: "club mix", "dance
  mix", "extended mix", "ultra mix", "long version", "biomix"
  (2026-10-21: "The Safety Dance Extended Club Mix" is the 4:32
  extended version, not the studio cut).
- `TRIALS_PER_SOURCE` default 5 (env `TUNESTER_TRIALS`).
- **CD/album track over music video** (2026-10-21): `pick` sorts
  `(-media_tier, score, popularity, -posted)` where `_media_tier` reads
  the candidate URL extension - 0 = lossless (FLAC/WAV, a CD rip),
  1 = lossy or no extension, 2 = video (MKV/MP4/...). So the standard
  CD/album audio-native track is tried before a music-video's audio
  track, and lossless before lossy. Only archive.org candidates carry a
  real file extension (nzb IDs and soundcloud track URLs are neutral),
  and nzb already prefers an audio file within a release at download.
- **Age preference**: nzb candidates carry the release post timestamp
  (`Candidate.posted`, from the feed `<pubDate>`); after the media tier
  and relevance `pick` breaks ties oldest-first, so OLD established
  releases outrank fresh uploads - new uploads' names/tags have proven
  unreliable (2015/2025/2026 "Mellow Gold" re-uploads vs the 2011-2013
  rips). The album-pluck `_rank` uses the same oldest-first tiebreak.
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
  2026-10-21: a NON-empty LLM album is also checked - `_mb_release_exists`
  asks MB whether the artist has a release by that name, and a REFUTED
  album (qwen returned "Ruckus", the label, for Men Without Hats) is
  re-derived from the tally above, since the album query is how original-
  album CD rips are found. An API failure (None) keeps the LLM's album.

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
response wins. Order (edit `_llm_endpoints()`): 1) **q38fa** (llama.cpp
llmhost `llm2.example.com/v1`, model `q38fa-iq3_xxs-SC117_abl` — the
qwen3.8-flash-next alias served as `qwen3.8-flash-next-iq3_xxs`,
131k ctx; same provider/key as omp's `models.yml` `llmhost`, wired via
`TUNESTER_LLM_PRIMARY_*` in `.env`; switched from the old
qwen38-27b Q6_K GGUF primary 2026-11-08 after that endpoint + llm1
both began 504-storming GrabbyTube's batch) 2) **llm1** (env
`OPENAI_*`; healthy ~9 s, observed 56-379 s under load 2026-09-27).
Non-final backend budget `FAILOVER_BUDGET`=90 s (hung endpoint); a dead
endpoint (refused/DNS/reset, `_DEAD_REASONS`) fails over immediately;
the last fallback keeps `LOAD_WAIT`=360 s cold-start wait.
`CHAT_TIMEOUT`=240 s. The primary has its own bearer
(`TUNESTER_LLM_PRIMARY_API_KEY`, falls back to `OPENAI_API_KEY`). Gate
degrades to filter-only if ALL backends fail.
- 2026-10-21: BOTH backends flaked mid-run (qwen38-27b exceeded the 90 s
  failover budget on a 12 s prompt; llm1 returned HTTP 504) and
  `discover` raised "all LLMs failed". A `--no-llm` run degrades to
  heuristic parse + filter-only verify - the probe/duration/AcoustID
  gates still run, but the LLM version gate (the only defense vs a
  clean-titled cover/karaoke) is OFF, so prefer waiting for the
  backends to recover over delivering `--no-llm` output.

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
- Men Without Hats – The Safety Dance (1981): 165 s, 160 k CBR
  (2026-10-21) - track 06 of the "Rhythm of Youth (1982)" FLAC (CD) rip
  on archive.org (lossless CD/album track, preferred over the official
  HD music video by the media-tier ranking). AcoustID matched the
  genuine MWH entry at 91-100%. The nzb release was only an expired
  "Extended Club Mix". Two gates were fixed to make this deliverable:
  (1) AcoustID's top cluster ranked a misattributed "SIRCH - Safety
  Dance" entry first, so `_acoustid_identify` now scans the WHOLE best
  cluster for a target match (was `recordings[0]`-only); (2) the LLM
  returned album="Ruckus" (the label), refuted by MB, so `discover`
  re-derived "Rhythm of Youth" from the tally - that album query is what
  surfaced the FLAC (CD) item. See the karaoke blind-spot + source-
  priority notes (2026-10-21).
