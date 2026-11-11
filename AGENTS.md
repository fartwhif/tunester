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

## Concert vs CD-rip sourcing (Oingo Boingo, 2026-11-10)
"many of the Oingo Boingo tracks ended up tuning to live tracks". Four
delivered files proved four distinct leaks, each needing its own fix:
- `Just Another Day` 361.9 s from `ob1995-10-20.ecm-939` (concert taper),
  `When the Lights Go Out` from `ob1990-10-27.sbd` (soundboard), `Wild Sex`
  from `ob1986-12-31.ecm-939`, `Skin` 268.9 s from `ob1993-01-08.aud`
  (Warfield taper) — all from dated `*.aud/.sbd/.ecm` archive items.
- A taper's per-file names carry the song title exactly like a rip
  (`339 Just Another Day.flac`), so per-file filters cannot separate a
  concert upload from an album rip and tapers outrank studio items.
  `_archive_is_live_item` (sources.py) now drops the WHOLE item before any
  file is considered, keyed on identifier + title + creator ONLY — never the
  description (every reissue mentions a concert) and never the separate
  `date` FIELD (a commercial album carries `1985-10-28` there; Dead Man's
  Party does). A taper instead encodes its show date in the identifier/title
  (`ob1993-01-08`, `Live at the Aquarius`, venue+date) or a radio/DJ creator
  (`PoP dEFECT RADIO`); "live" is exempt when the artist is literally named
  Live, mirroring `make_bad_filter`. Verified 0/15 mismatches on the live
  Boingo items + a "Live"-band control. The item title is also passed to
  the gate as `source_collection` so a same-length taper the classifier
  misses (Skin is within the 15 s anchor tolerance of its 283 s target) is
  still refused.
- The duration anchor ran only for candidates WITHOUT a self-reported
  length (`if not cand.duration`), i.e. nzb IDs alone; an archive file
  carries its own `tracklen`, so a 362 s taper compared 362 against its own
  362 and never against the 314 s studio anchor. The anchor is now
  unconditional; `cand.duration` survives only for the truncation check.
- The typed year was discarded: `_requested_title` strips `(1985)` and
  nothing pinned it, so the LLM's nondeterministic year (returned 1982 for
  Just Another Day) drove the ±1 album window and the anchor. `_requested_-
  year` re-reads the trailing `(YYYY)` (present ONLY in the explicit
  `Artist - Title (YYYY)` form, so a free-form "...from 1986" is untouched)
  and keeps it as ground truth. With 1985 the anchor populates (314 / 229 /
  283 s measured) and the studio album derives.
- **Apostrophes defeat the album query on BOTH sources.** MB returns the
  curly `Dead Man’s Party`; nzb.life ANDs on the apostrophe, so
  `Dead Man's Party` returned only 3 loosely-matched NMR/bootleg docs and
  MISSED `Oingo Boingo-Dead Mans Party-WEB-1985-MARR` (the scene studio rip,
  5 WEB hits once stripped); archive returned 25 podcast/taper noise docs
  and missed the studio items `dmp-remastered-expanded-edition`,
  `oingo-boingo-dead-mans-party_202609`, `02-dead-mans-party` (which hold
  `01 Just Another Day.flac`, `09 Weird Science.flac`, …). `_query_text`
  strips straight + curly apostrophes from the OUTGOING nzb and archive
  query strings only; the stored album keeps it (compared only via `_norm`).
  Apostrophe-free albums (`Nothing to Fear`, `Only a Lad`) were always fine
  — which is why non-`Dead Man's Party` Boingo songs already delivered right.
`Weird Science` (138.4 s) is a separate wrong-version: the studio SINGLE/radio
edit (~2:18), which the duration anchor now vetoes against the 229 s album cut
that `02-dead-mans-party`/`dmp-remastered` carry.

- **The typed-year pin does NOT cover the real GrabbyTube flow, which
  sends NO year.** `GrabbyTube/tunester.py:104` submits exactly
  `"author - title"` with no `(YYYY)`, so `_requested_year` is empty for
  EVERY track and the only year left driving the ±1 album window and the
  duration anchor is the nondeterministic LLM guess (1979/1982/1985/1986
  across runs for the same six titles). A wrong year empties
  `musicbrainz_album`'s window so the studio rip — reachable ONLY through
  the album query — is never found and a concert take wins. Fix: when NO
  year is typed, override the LLM year with `musicbrainz_studio_year`,
  MB's date of the FIRST studio album the song appeared on (earliest
  exact-title recording carrying a NON-live album release; live groups
  like "Boingo Alive" / "13 January 1993, Universal Amphitheatre" /
  "Farewell: Live…" are skipped, and the original album always predates
  its own compilations, so earliest == original). Objective, not the
  LLM's coin-flip. Measured: JAD/Weird Science/NOLF -> 1985 (Dead Man's
  Party), Skin/When the Lights Go Out -> 1990 (Dark at the End of the
  Tunnel), Wild Sex -> 1982 (Nothing to Fear); anchors then populate at
  the true studio lengths 314/229/254/283/257/251 s. The override fires
  ONLY with no typed year (a free-form "...from 1986" still defers to the
  LLM, and an explicit `(YYYY)` still pins, as above).
- **The ACTUAL reason the album was always empty was a silent
  positional-argument regression, NOT the year and NOT rate limits.**
  `Target`'s fields went `artist, title, year, album` -> `discover` line
  `target = Target(artist, title, year, album)` was a correct 4-positional
  call. On 2026-10-22 commit `6f43645` ("Tag delivered files with
  cd_year") inserted `cd_year` as the 4th field, pushing `album` to 5th,
  but left that call unchanged. So `album` was bound into the `cd_year`
  slot and `target.album` stayed `""`; the very next line
  (`target.cd_year = cd_year`) then overwrote the misplaced album. Net:
  `target.album == ""` for EVERY `discover()` since 2026-10-22, so the
  album pluck — the ONLY route to a studio rip (a studio track does not
  appear in any title-matched archive item: JAD's title-only
  `_archive_search` returns 0, the album query returns
  `01 - Just Another Day.flac`) — silently never ran for ANY song, and
  everything fell back to title-search concert tapers. Fix: pass album BY
  NAME, `Target(artist, title, year, album=album)`. Caught because
  `musicbrainz_album(...)` returned `'Dead Man's Party'` on 5/5 calls
  while `target.album` was `''` — the return value was correct but
  discarded; the contradiction pointed straight at the assignment slot.
  With the slot fixed, JAD delivers the scene studio rip
  `Oingo Boingo-Dead Mans Party-1985-EOS` (nzb album-pluck -> llm gate ->
  313.1 s, within the 314 s anchor).
- **`_mb_get` is rate-throttled (1.2 s spacing + 4× backoff)** as
  hardening, not the live-rip fix: a single tune now makes 5 MB calls
  (studio_year + album + cd_year + duration + the release-exists check),
  and MB's budget is ~1 req/s per IP with the host and the container
  sharing the box's single egress IP. The throttle spaces them and backs
  off 429/5xx up to 4 times so the album backfill survives load; it was
  NOT the cause of the empty album (that was the slot bug above) — the
  429 explanation was a false lead this session's probes disproved.
- **Once the album pluck runs, the audio-blind LLM gate then invented
  "live" for real studio rips.** It judges only the release file name, so
  a scene rip named `Oingo Boingo - 1990 - NMR - Dark at the end of the
  tunnel` (or `.../02 - Skin.flac` inside it) read to the model as a live
  album — but MusicBrainz types *Dark at the End of the Tunnel* (1990) as
  a STUDIO album (it is exactly what `musicbrainz_studio_year` derives for
  Skin / When the Lights Go Out). Both were rejected as "live" despite
  AcoustID matching `Oingo Boingo - Skin` 98% and `When the Lights Go
  Out` 96% with NO live marker, and lengths inside the 283 / 251 s studio
  anchors. Fix: a `version=live` veto is the one the QC gate can override
  with evidence. AcoustID already *rejects* a fingerprint that identifies
  a live recording, so a high-confidence match to a NON-live recording
  (`studio_confirmed`) PLUS a length matching the anchor (`anchored`) is
  content proof the name-reading model lacks; `verify_download` delivers
  the rip (`gate="acoustid"`). It fires only on that conjunction — a real
  taper fails the length anchor and stays rejected — and is checked
  BEFORE the `match` flag, because the QC prompt tells the model to answer
  `match=false AND version=live` for a live album, so the name-based claim
  arrives with `match=false` and a veto placed after the match check never
  ran (a real bug this session caught in the first re-run). Does NOT
  override `clean/radio/cover/remix` vetoes or a `version=unknown` reject
  (Weird Science's `snacks version`): only the live claim is fingerprint-
  refutable. `unknown` still refuses — when unsure, match=false stands.

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
