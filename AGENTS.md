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
  whole-RAR plucks: it scans sidecars for this release URL + a name naming
  the target title at the same `_title_hit` tier the pluck would have used
  (see "Parenthetical-stripped tier" under Album pluck), so a different
  track from the same release is a miss.
- `deliveries/` — key `sha1(_norm(artist), _norm(title))`, i.e. the song
  identity the gates verified. **The tag year is NOT in the key**: which year
  a pre-CD song first hit CD is fuzzy and `discover` does not answer it the
  same way twice (observed: "Nena - 99 Luftballons" → 1983, 1984 and 1986
  inside an hour, landing three copies in `downloads/`), so a year-keyed
  store re-downloaded the same recording on nearly every repeat. The year is
  in the sidecar and drives the file name: on a hit the **stored** name is
  used (`_delivery_name`), so file name and ID3 date always agree. Hit =
  search/fetch/gates/transcode all skipped; the copy is verified by
  `check_output`, the ±15 s comparison against this run's `mb_duration`
  anchor, AND the full AcoustID objection rules (2026-11-11, see the
  re-verification note below) before it lands (a same-title entry that is
  a different cut is a miss, not a substitution; `Result.gate == "cache"`).
  A spec failure, a ruled-out cut, or a fingerprint objection drops the
  entry so the fresh delivery replaces it; a bad *copy* of a sound blob is
  kept. Seeded from `downloads/` by `app.py --seed-cache` (parses
  `ARTIST - TITLE (YEAR).mp3` names, runs `check_output` and the
  fingerprint mismatch/live check on each — a seeded library file is
  unverified user input — counts stored / skipped / already-in-store).
  Since the 2026-11-13 wrapper engine the key risk is smaller but
  real in a new way: every RECOGNIZED label group (venue, remaster,
  format, rendition) is stripped before discovery, so the old
  `(Live at …)`-carries-a-genuine-live-recording case can no longer
  reach the key as a title — MB-titled live-only songs now key as
  their bare title. A credit strip goes further: `Dark Horse` keys
  the same entry for the album cut AND for the recording MB titles
  `Dark Horse (feat. Juicy J)` — the first-verified variant serves
  both. The 2026-11-11 AcoustID re-verification on hits (fingerprint
  objection + the ±15 s anchor comparison above) is what keeps a
  wrong-variant hit from being delivered as-is. Residual known limit:
  a parenthetical **subtitle** the ladder confirms or peels
  differently run to run (MB unreachable) — "I Wanna Dance with
  Somebody" vs "(Who Loves Me)", 2026-11-10 — is a separate entry
  and a second file.
`--no-cache` = `Options.use_cache=False` skips reads AND writes in both
stores. `_cache_evict` caps both subtrees at `TUNESTER_CACHE_MAX_MB`
(default 30720) by entry mtime; a hit refreshes mtime so used entries
survive. Every cache failure (unwritable dir, corrupt blob/sidecar, copy
error) logs and returns a miss — a cache problem NEVER fails a tune.

## Fingerprint re-verification (2026-11-11)
The AcoustID gate used to run only on fresh downloads, so it had three
blind spots found while auditing the Oingo Boingo / Blink-182 deliveries:
(1) a `deliveries/` cache hit skipped the fingerprint entirely — an entry
could enter the store through `--seed-cache` from a library file whose
audio was never content-checked (the Blink-182 "I Miss You" seeded from a
file whose actual audio was the Vevo master of a different song), and (2)
`_acoustid_identify` treated every HTTP failure as "gate unavailable"
without looking at the AcoustID **status** field, and AcoustID answers
`{"status":"queued"}` (202/429: fingerprint seen, result not computed
yet) — reading one as "no results" would condemn perfect deliveries under
the new zero-coverage rule, so the lookup distinguishes the statuses,
(3) a live marker sitting in the matched recording's disambiguation
comment (bare AcoustID title) was read as a studio match.
`_acoustid_lookup` returns the cluster list (possibly empty) only on
`status == "ok"`; `queued` / "out of service" (under-load answers that
used to be misread as "no results") is retried with backoff, and None —
"the service never answered" — comes only from an error status (bad key),
a transport failure, or retries exhausted. `_acoustid_identify` then
returns the verdict dict or None (no key, fpcalc failure, no answer):

  `matched`: (artist, title, disambiguation, conf) of the highest-scoring
             recording, across ALL clusters, whose artist+title match the
             target — a misattributed entry can rank first in the cluster
             (Safety Dance / SIRCH, 2026-10-21). All recordings in a
             cluster describe the SAME fingerprinted sound, so equal
             scores are a tie between attributions, not evidence: the
             **non-live** attribution wins the tie (higher score still
             wins outright). The Blink-182 "I Miss You" studio master
             scores 0.985 against a cluster that lists "I Miss You (live
             in Minneapolis)" FIRST and five bare studio recordings after
             it; first-wins picked the live name and vetoed the proper-CD
             FLAC pluck as a live take (2026-11-11) — a near-identical
             live take is routinely linked into the studio cluster, so
             attribution order, not score, decides studio-vs-live at the
             top of a cluster.
  `best`:    top cluster score 0..1
  `nrecs`:   distinct catalogued (MusicBrainz-linked) recordings seen;
             `best == 0 and nrecs == 0` is zero coverage — nobody has
             these bytes on AcoustID; `best > 0 and nrecs == 0` (audio
             seen before, no catalogued identity) is NEUTRAL
  `top`:     "artist - title" of the first catalogued recording (the
             reject reason's identity)

`_acoustid_objection(ident, target, expect)` is the single reject rule,
now used in ALL THREE places — fresh download (`verify_download`), cache
hit (`_delivery_get`), seed (`seed_cache`):
- `matched` at conf ≥ `TUNESTER_ACOUSTID_MIN_CONF` whose title OR
  recording disambiguation comment carries `\blive\b` (target not itself
  live) → reject. The comment scan matters because AcoustID titles are
  often bare (Oingo Boingo studio tracks match as plain "Try") while the
  live marker sits in the comment; `\b` keeps "No One Lives Forever"
  from a false hit. Because identify keeps the non-live attribution on a
  score tie, the veto fires only when NO studio-titled attribution scores
  the same as the live one — a genuine live take keeps its veto (its own
  cluster outranks the studio match), and where duration is gated it
  fails the ±15 s anchor before the fingerprint even runs.
- `matched` at any other conf → pass, logged "studio/live not proven"
  (and it does NOT set `studio_confirmed`, so the LLM's invented "live"
  veto is not overruled on a weak match).
- no match, `best` ≥ MIN_CONF with catalogued recordings → reject
  "identifies as <top>".
- `best == 0 and nrecs == 0` with `expect > 0` (MusicBrainz catalogues
  the song) and `TUNESTER_ACOUSTID_REQUIRE` on → zero-coverage reject: a
  catalogued famous song always has submissions, so bytes nobody ever
  submitted are not that recording — this is what condemns the Blink-182
  "I Miss You" Vevo swap. `expect == 0` or `REQUIRE=0` keeps it a
  warning.
`expect` is the MusicBrainz studio anchor (`target.mb_duration`,
year-aware from `discover`); at seed time it is 0 — no discovery ran, so
zero coverage cannot be judged there — a seeded file with no fingerprint
coverage is seeded and caught on its first cache hit, where the anchor
exists. On a cache hit an objection DROPS the entry and the run re-tunes
(self-healing: the replacement delivery is stored); no anchor + no
coverage serves, because an AcoustID/MB failure must never condemn the
store. `verify_download` keeps its own year-aware `expect` (consensus
fallback when the target has no year) and the `studio_confirmed +
anchored` overrule of an LLM "live" veto.

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
free-form descriptions return "" - there the LLM has the wheel) - the
name `discover` hands them has ALREADY been cleaned by the wrapper-junk
engine (see the next two sections), and `_requested_year` simply returns
the trailing year group `_unwrap_junk` captured. The requested title is
additionally cleaned of a repeated artist prefix ("Gorillaz - Gorillaz -
Feel Up (HD)" -> title "Feel Up"), and a LISTING-style title whose song
sits in quotes ('"Bad Blood" Ministry', '"Lonesome Dove" Live at ...
2025') yields the QUOTED CORE plus any groups sitting right behind the
quote: what trails the quote is either an artist credit - in which case
`_scrub_tracklist` already rewrote the whole name and this never sees
it - or unbracketed venue/date noise no group rule can see (the quoted
text IS the song either way), while a bracketed tail ('"Guaranteed"
(From Into the Wild)') is an identity candidate the MusicBrainz ladder
must still judge, so it stays.
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

## Wrapper-junk engine (`_unwrap_junk` / `_group_verdict`, 2026-11-13)
Uploading platforms wrap label junk in ANY bracket family - `(Radio Edit)`,
`[HD]`, `{Remastered}`, `"Video"`, «Live», 『Official Music Video』,
【320 kbps】 - so the wrapper CHARACTERS are not the rule; the group's
CONTENT is (owner's decisions, ask form 2026-11-13):
1. an unrecognized group survives ONLY when MusicBrainz titles one of the
   artist's recordings exactly that way (`_mb_confirm_title`);
2. families `( ) [ ] { } < > « » ‹ › “ ” „ " 「 」 『 』 （ ） ［ ］ 【 】`,
   same rules everywhere; curly SINGLE quotes are deliberately NOT
   wrappers - an apostrophe ("Don't Stop Me Now") must never read as an
   opened group;
3. labels strip ANYWHERE in the name, not only at the end;
4. `feat.`/co-artist credits strip - the target is the bare song, and the
   MusicBrainz helpers still resolve the credit-carrying recording
   (`_rec_title_ok` below) instead of re-crediting the target;
5. venue / dated-live groups strip too (`(Live at the Apollo)` -> gone):
   the deliverable is the studio recording, and a live-only song carries
   the bare title on its own tracklists anyway;
6. only a TRAILING bare year group is the typed release year; a year group
   anywhere else is noise the engine drops;
7. cleaning happens BEFORE everything, the identify LLM included: `discover`
   scrubs the tracklist form, then `_unwrap_junk`s the name ONCE, and the
   LLM prompt, the pins, every MusicBrainz lookup, the search queries and
   the cache key see only the cleaned name.
Carried-over invariants: the deliverable is the original uncensored studio
recording; `(Explicit)` stays (the explicit cut IS the deliverable); a
group is NEVER peeled when only the label would remain - `(Live)` unwraps
to `Live` (a band and a song title), `(Everything I Do)` unwraps to its
content, while `Song (Live)` -> `Song`.

`_groups_with_cuts` is the scanner: balanced groups per family (a closer
matches only its own opener), nesting-aware; a group that opens ANOTHER
family inside itself is TANGLED - its content is never verdict-dropped,
because verdict words inside it describe the INNER structure (so
`'"Song (Live)"'` keeps the quoted song name; the ladder peels it like any
identity group). Unclosed openers are ignored. `_group_verdict(content)`
judges content, family-irrelevant BY DESIGN:
- `format`: every token a quality/format word AND ≥1 anchor (`(HD)`,
  `(320 kbps)`, `(Official Music Video)`, `(1080p)`) - checked BEFORE the
  rend-last rule, because 'hd' is itself a marker word;
- `remaster`: mentions a remaster AND every token is reissue noise -
  `(2011 Remastered)`, `(Remastered HD)`, `(30th Anniversary Remastered
  Deluxe Edition)`, `(2011 Remaster of the Original Recording)` peel,
  while a title genuinely carrying the word (`Song (The Remastered
  Master)`, `Born to Remaster`) stays;
- `rendition`: LAST token is a marker word - named remixes are open-ended,
  the marker word is not: `(Kurayamino Mix)`, `(Hot Rod Herman Remix)`,
  `(Boingo Alive Version)`, `(Radio Edit)`, `(24 Carat Bootleg)` drop;
  `(Kurayamino Variation)` / `(Y2K Reload)` / `(Extended Play)` end in
  ordinary nouns and go to the ladder instead;
- `credit`: `feat`/`ft`/`featuring`/`duet`/`vs` first, or `with` as the
  SECOND token - `(Duet with Phil Collins)` is a credit but
  `(A Deal with God)` merely contains the word and stays (decision #4);
- `venue`: carries live/concert/recorded/performance/festival/session
  (`(Live at the Apollo)`, `(Live 1985)`); a bare `from`/`at` group with
  no live marker is NOT venue - the ladder judges it;
- `year`: a single 4-digit token;
- `keep`: everything else - and `(Explicit)` is protected outright.
`_strip_groups` drops verdict groups one at a time, rightmost first, until
stable, so inner and newly-exposed groups get judged too; `keep` and
TANGLED groups survive to `_mb_confirm_title`, which queries
`recording:"TITLE" AND artist:"ARTIST"` and peels the rightmost group
(max 5 queries) until MusicBrainz has a recording titled exactly that:
`(Shove It)`, `(Kurayamino Variation)` and `(You Gotta) Fight for Your
Right (To Party!)` confirm on the first query, fakes like `(Y2K Reload)`
peel down to the bare song. MusicBrainz unreachable -> keep what the
verdict engine left.
`_rec_title_ok` is the exact-title test the ladder AND every MusicBrainz
helper (studio_year / album / cd_year / duration / listing_artist /
album-credit names) share: normalized equality, or - only for a BARE
target - the target plus CREDIT groups, so the credit-stripped `Dark
Horse` still resolves MusicBrainz's `Dark Horse (feat. Juicy J)` while a
recording differing by a remaster/rendition/venue/year group is a
DIFFERENT recording - that is what keeps the studio anchor off the 12".
`_core_title` (the album-pluck tier-1 core) drops groups of every family,
inert unless ≥8 normalized characters survive. The remaster-label rule of
the previous engine (`(Remastered)`/`(2011 Remastered)`/`(Digital
Remaster)` noise vs `(Remix)`/`(Radio Edit)` identity) survives INSIDE
this vocabulary as the `remaster` and `rendition` verdicts; the old
trailing-only position rule and the `(Live)`-identity exception do not
(owner decisions #3/#5).
Verified offline: t1 (18 typed pins), t2 (16 remaster/unwrap cases) and
t5 (36 group verdicts - stacked labels in any order, the unwrap-when-empty
guards, `(Alive)` / `(Extended Play)` / `(Never Live)` edge verdicts)
0 failures in the container.

## Tracklist-form upload names (`_scrub_tracklist`, 2026-11-13)
YouTube compilation / film-soundtrack channels name uploads like the
LISTING they came from: the channel takes the artist slot and the real song
sits in quotes with the real artist right behind it —
`Flix Soundtracks - The Matrix Soundtrack Track 9. "My Own Summer (Shove
It)" Deftones`. Naive `Artist - Title` parsing reads `Flix Soundtracks` as
the artist and the whole listing line as the title, and because `discover`
PINS whatever the user typed (see above) that junk propagates into the
DISCOVER prompt, the pins, the MB lookups, the search queries and every
gate — all 12 remaining Matrix tracks tuned to nothing with
`artist="Flix Soundtracks"`. Fix: scrub the name to the canonical
`Artist - Title` at the TOP of `discover`, before any parsing, so the whole
chain works on the real song (and the fixup GrabbyTube writes back is the
clean name too).

Three conditions must hold, because a false scrub retargets the whole run
at the wrong song:
1. a quoted title with a plausible artist credit IMMEDIATELY behind it
   (`_QUOTE` = double quotes only — an apostrophe must never close the
   title, or `Queen - Don't Stop Me Now` would parse; the tail must pass
   `_ARTIST_SHAPE` and carry no `live at`/`feat.`/`official`/`video`/`cover`
   /`explicit`/… marker and no 4-digit year, so the user's own `(1994)` tag,
   `(Explicit)` and `Live at the … Festival 2025` all fail it);
2. the name identifies itself as a listing — a `Track 3.`/`Disc 2:`/`Side
   A`/`Part 1` marker, or a label ending in `-`/`:` before the quote (how a
   channel fronts its own name);
3. MusicBrainz confirms that artist for that EXACT title
   (`musicbrainz_listing_artist`, matching per artist-credit NAME so a
   duet/featured credit still resolves, and a short tail resolves to the
   canonical spelling: the Matrix listing's `Rob D` is MB's `RoB-D`, the
   name every rip is filed under). An unconfirmed tail is not an artist
   credit → no scrub. **`limit=100` is REQUIRED**: `Rock Is Dead` has 100+
   MB recordings and Marilyn Manson's are not in the first 25, so a small
   limit reads as "no such recording" and silently skips the scrub.
MB unreachable (None) still scrubs on shape alone — a better guess than the
listing, and every MB lookup downstream is off anyway.
The quoted text passes through the SAME verdict engine as a typed title:
`(Shove It)` stays (verdict keep, and MusicBrainz-confirmed), while a
rendition or reissue label inside the quotes strips exactly as it would
outside them - `(Hot Rod Herman Remix)`, `(Kurayamino Mix)`,
`(2005 Remastered)` drop - so the scrubbed name is always the
bare-identity title MusicBrainz actually uses: `"Bad Blood (2005
Remastered)" Ministry` -> `Ministry - Bad Blood`,
`"Clubbed to Death (Kurayamino Mix)" Rob D` -> `Rob D - Clubbed to
Death`. The other normalization is whitespace (`\s+` -> one space),
because YouTube puts a nbsp inside these names (`Clubbed to Death\u00a0
(Kurayamino Mix)`).

Verified offline (t4, 0 failures): 16 scrub controls stay untouched
(Reed Waddle `"Lonesome Dove" Live at … 2025` — the venue tail fails
condition 1, and `_requested_title`'s quoted-core rule extracts
`Lonesome Dove` downstream; `Queen - Don't Stop Me Now (1975)`,
`(Explicit)`, `Artist - "Title" - 1999`, `Various - "Bad Blood"`,
bracket-file names, `Artist - "Track 5" (Remix)`) and 5 dirty names
scrub to the canonical form, wrong-artist tails rejected by MB
(`"Lonesome Dove" Reed Waddle`, `"Bad Blood" Foo Fighters`).
E2E after the 2026-11-13 deploy — the three RAW polluted listings
through `/api/tune`: `"Bad Blood" Ministry` scrubs to
`Ministry - Bad Blood` (MB studio-year 1999 + the Matrix OST album
tally, 300 s anchor, cache-hit studio delivery), `"My Own Summer
(Shove It)" Deftones` scrubs with `(Shove It)` intact and delivers
from the store, `"Mindfields" The Prodigy` scrubs to
`The Prodigy - Mindfields (1997)` (276 s anchor) and fails honestly —
every candidate gate-rejected (zero AcoustID coverage / 230–337 s vs
276 s) — so the job still returns the clean artist/title and
GrabbyTube writes the fixup. Through GrabbyTube `/tracks/tunester`
(9 catalog tracks, names already fixup-cleaned by the earlier batch):
6 verified deliveries — `Rob Zombie - Dragula (1998)` (the Hot Rod
Herman Remix fixup replaced by the studio cut), `Kaskade - Raining
(2014)`, `Ferry Corsten - Fire (2005)`, `Katy Perry - Dark Horse
(2013)` (credit stripped), `Reed Waddle - Lonesome Dove` (the venue
tail dropped, a verified non-live take), control `Leftfield - Open Up
(1994)` unchanged — and 3 honest fixups with clean names
(`Melle Fresh - Cocktail Queen`, `Oingo Boingo - Goodbye-Goodbye
(1982)`, `Rob Dougan - Clubbed to Death (1999)`). No `Flix
Soundtracks` string appears in any job, delivery name, or catalog
entry.

## QC gates (`verify_download`)
probe (ffprobe: decodable, ≥20 s, not truncated) → **duration anchor** →
**AcoustID content gate** (optional) → **LIVE-version gate** → LLM version
gate (any non-studio version → reject).

- **AcoustID content gate** (`_acoustid_identify`, needs
  `TUNESTER_ACOUSTID_KEY`; fpcalc = `libchromaprint-tools`): fingerprints the
  download (chromaprint) and looks it up on AcoustID via the **v2 API**
  (`POST https://api.acoustid.org/v2/lookup`, param `client`, `meta=recordings`;
  the result `score` on a **0..1** scale; `meta=recordings` brings each
  recording's disambiguation comment. Every cluster is scanned: the
  highest-scoring recording whose artist+title match the target is
  `matched` (a misattributed entry can rank first — Safety Dance / SIRCH,
  2026-10-21), its conf ≥ `TUNESTER_ACOUSTID_MIN_CONF` (default **0.7**)
  is the strong pass. With no match, a top catalogued recording at
  ≥ MIN_CONF rejects with gate `acoustid` — the ONLY gate that catches a
  mislabeled article (a file named/tagged "Loser" that contains a
  different song of the right length passes every name/duration/LLM gate;
  Beck "Loser" 2026-09-28 pitfall below). Anything weaker is
  inconclusive: logged, no verdict, the LLM gate decides. 2026-11-11: the
  lookup is **status-aware** (a `queued` answer is retried, never read as
  "no results"), zero coverage on a MusicBrainz-catalogued target is a
  rejection, and the same objection rules now run on cache hits and on
  `--seed-cache` — see the Fingerprint re-verification section. Key = the
  user's API key from acoustid.org (set in `.env`); silently skipped when
  the key or fpcalc is absent.
- **LIVE-version gate (2026-11-03)**: a live take of the correct song by
  the correct artist at the correct length must NEVER be delivered - the
  deliverable is the original uncensored studio recording as it appears on
  the standard commercial CD. Two hard (non-LLM) checks:
  (1) AcoustID - if the fingerprint identifies the content as a LIVE
  version — `\blive\b` in the matched recording's title (e.g. "Bohemian
  Rhapsody (Live at Wembley)") **or in its MusicBrainz disambiguation
  comment** (`Push (live)`, `Drive (live at Edgefest 2006)`; the AcoustID
  title is often the bare song name, the marker lives in the comment) —
  at ≥ MIN_CONF, it is rejected with gate `acoustid`, even though the
  match test passed (the target title is a substring of the live title).
  Added 2026-11-11: the comment scan, and the same veto now applies on
  cache hits and seeds, where it used to fire only on fresh downloads. A
  target that IS itself a live recording is exempt (rare). Word-boundary
  match, so "No One Lives Forever" and "Livin on a Prayer" are not
  flagged.
  (2) LLM version vocabulary - the LLM's `match` flag is NOT trusted on its
  own: `version` in {clean, radio, live, cover, remix} rejects regardless of
  `match` (the LLM was observed answering match=true while labeling the
  version live). `VERIFY_SYSTEM` also states a live performance is never
  acceptable. Name-level live is already dropped by NEVER_WORDS ("live")
  except when it is part of the artist name (band "Live").
- **Karaoke/instrumental blind spot** (2026-10-21): derivative audio
  (karaoke backing tracks, instrumentals) is largely absent from the
  AcoustID catalogue — bytes nobody submitted read as zero coverage,
  bytes somebody did submit carry no MusicBrainz identity — so a
  clean-titled karaoke upload can still slip the content gate: when the
  target is MB-catalogued the zero-coverage case now REJECTS (added
  2026-11-11, see the Fingerprint re-verification section), but coverage
  that carries no catalogued identity stays neutral and only the LLM
  gate stands between a troll upload and the delivery - no text-only gate
  can see that vocals are missing (Men Without Hats "The Safety Dance":
  the nzb release was an
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
and up to 10 are NZB-checked (cache-first `_nzb_manifest`); EVERY release whose file list
contains a clean file naming the song becomes a pluck candidate, best
first, and each is tried until one survives the duration/AcoustID/LLM
gates — **before** direct search hits (the original album is the canonical
source; direct hits remain the fallback). Returning only the first manifest
match was wrong: a release can pass the name check and still die at the
download stage (archive over the size cap, bad rip), and the next album
pressing is the fallback wanted, not a generic search hit.

**Parenthetical-stripped tier (2026-11-12)**: `_title_hit(name, target)`
ranks a file name 2 = carries the target title, 1 = carries the title with
every bracketed segment of any family dropped (`_core_title`, inert unless ≥8
normalized characters survive), 0 = neither. Tier 1 exists because **album
rip track files carry the SHORT form of a song whose single/MusicBrainz
title is long**: Licensed to Ill names track 7 `Fight for Your Right` while
the single and MB recording 026c26a7 carry `(You Gotta) Fight for Your
Right (To Party!)`, so no containment test preserving the parentheticals
can ever match — the pluck checked 10 releases (including three
per-file CD rips: `07-beastie_boys-fight_for_your_right-rec.mp3`,
`07 - Fight For Your Right.flac`) and reported "no release contained the
track", while the whole direct-search fallback had only a 182 s non-studio
candidate. Tiers, not one loosened test, so a full-title file always
outranks a stripped-name file in the same release (a 12" named after its
A-side is still never plucked for the B-side); `_nzb_album_file`, the
bare-file and archive pluck rankings, and the release-level cache lookup
`_cache_release_hit` all use the same tiers so pluck and cache agree.
Delivery: the 24-bit/96 kHz 2014 PBTHAL vinyl FLAC (`07 - Fight For Your
Right.flac`, 212 s) → AcoustID 100% `Beastie Boys - (You Gotta) Fight for
Your Right (To Party)` → LLM studio → 211.8 s 160k delivery replacing the
fake in library `eBShN8qT4lk`.

**Co-artist albums (2026-11-12)**: the album is also searched under the
artists MusicBrainz credits its pressings to (`_album_credit_names`, top 2
by pressing count, 'Various Artists' + the target's own name spellings
excluded). Scene releases are titled `Artist-Album-...` with the ALBUM's
artist, and a duet or featured track sits on the co-artist's album, so the
requested artist can never find it: "Phil Collins - Easy Lover" is track 6
of Philip Bailey's "Chinese Wall" (the recording is credited to 'Philip
Bailey & Phil Collins'; the CD rip on nzb.life is
`Philip Bailey-Chinese Wall-CD-FLAC-1984-CALiFLAC` and the track file is
`06-philip_bailey-easy_lover_(duet_with_phil_collins).flac`). "Phil Collins
Chinese Wall" returned nothing, the album cut stayed unreachable, and the
library kept a 349 s zero-coverage cover take (delivered 2026-11-10, before
the zero-coverage reject existed, which is what the user heard as "some
random dude pretending to sing it"). After the fix the pluck finds the rip
in 33 s and the delivery is the 306 s album cut, fingerprint-confirmed
against 'Phil Collins - Easy Lover' at 0.9889. The gate stack already
handles the co-artist credits: `_acoustid_identify` scans every recording
artist-credit in the cluster, and the LLM gate accepted the Bailey-credited
file name because the named file itself carries the duet credit.
## Last-resort compilation scan (`_nzb_comp_cands`, 2026-11-14)
When no normal source (and no album pluck) delivers a verified copy, the
pipeline browses **every nzb.life audio release credited to the artist**
(`nzb_artist_releases`, the whole catalog paged past the 100-item page cap)
and admits the ones whose CONTENT holds the song — judged by the NZB
manifest, never the release title. The release that carries the track is
named after the **comp** ('Greatest Hits', 'The Best Of + Vegas + Tweekend'),
which names neither the song nor the original album, and a greatest-hits may
not even be a real MusicBrainz album — so no `artist+album` or `artist+title`
query, album pluck included, can reach it. Runs only after the normal
`for src` loop is exhausted, strictly last ("last to keep things snappy").

**Content tiers, strongest first** (never position — tracklists lie and
tracks move; an admitted release is always downloaded and run through the
duration/AcoustID/LLM gates exactly like a normal source):
- **tier 2 — a media file in the manifest names the song.** The
  `Greatest Hits [RSD.2022 PBTHAL LP 24-96 FLAC]` box lists its tracks by
  name (`03..Self.Esteem.flac` = the user's "file number 3"); `_nzb_album_file`
  already reads this, so it reuses that. Album pluck skipped this release only
  because its gate demands the *original album* ('Smash') in the title —
  'Greatest Hits' never contains it.
- **tier 1 — no media file names it, but a tracklist sidecar does.**
  Hash-volume comps post every part as `...::HASH::/...`, so `_nzb_file_name`
  yields `(1/1)` for all of them and the box admits nothing by name
  ('Community Service', DAB live boxes, scene boxes whose audio is inside the
  RAR). `_nzb_sidecar_text` fetches the smallest small `.m3u`/`.m3u8`/`.cue`/
  `.foc`/`.txt`/`.nfo`/`.log` sidecar (≤512 KiB) as a single NNTP file — a few
  KB, not the whole box — and `_sidecar_track` applies the same `_title_hit`
  tiers to each line (`NN - Title` and bare `Title` both normalize). A 104
  cached manifests under the delivery cache exhibit exactly this no-bare-media
  + small-sidecar shape, so the tier is load-bearing, not hypothetical.

**Bounded and cache-first:** at most `NZB_COMP_CHECKS` (default 24) manifests
are read per run — getnzb is per-release and carries a 24 h 'Already
Downloaded' cooldown, so the scan must not hammer all 89 releases; a cooldown
or fetch miss just skips that release (the persistent `.nzb` cache makes a
retry free on the next run). Releases are de-duplicated by URL (one release is
posted to many groups), already-tried URLs skipped, censored/altered titles
dropped. The album name (when known) only **orders** the candidates — an
album-titled box is tried first as the likeliest clean rip — it never
filters, and there is deliberately **no year filter**: a 2022 reissue of a 1994
song prints the comp's own year, and the duration/AcoustID gates, not the
title year, decide whether it is the song.

**Shared trial body:** both the per-source loop and the scan feed
`_try_candidates`, which downloads → verifies → transcodes each candidate
until one survives every gate and records every attempted URL in `tried`
so the scan never re-fetches one. A confident match is a *verified file*,
not a title that looked right: the first name match does not end the search,
the first gate-passing delivery does.

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
  2026-11-10: the tally WINS whenever it returns a title — `_mb_release_exists`
  ("artist has SOME release named X") is deleted: it could not catch a
  wrong-but-existing album. The LLM put "Just Another Day" on "Weird
  Science" — the OST is a real Oingo Boingo release, exists() passed, yet
  it never contained the song: the album pluck found nothing and only
  distorted SoundCloud uploads were left. The tally only counts albums
  the exact-title recording ACTUALLY appears on, so an empty LLM album
  or any wrong guess yields to it; only an EMPTY tally (standalone
  single, API failure) keeps the LLM's album.

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
`Weird Science` is the anchor bug, not a search failure. It has TWO 1985
studio recordings: the album cut (~368 s, MB recording `0e84643f`, which
`Dead Man's Party` pressings carry at 367–368 s — the scene EOS rip and
`02-dead-mans-party`'s flac/mp3 all match) and a ~229 s radio edit
(`dfa94921`) that only ever appears on compilations/soundtracks, never as
the DMP album track. The recording-median anchor locked on 229 s (the edit
is reissued on ~25 compilations that out-vote the handful of album
pressings) and VETOED every genuine 368 s album rip as "too long"; the
138 s that shipped was a SoundCloud remix. See the album-pressing anchor
fix below.

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
- **The duration anchor is album-anchored, not recording-median.**
  `musicbrainz_duration(artist, title, year, log, album=)` now takes the
  length the track carries on pressings of the DERIVED original album
  (non-live release-groups only) — literally "the standard CD version".
  One such length is already decisive (an original pressing IS the
  standard cut); the within-15 s cluster only adds robustness across
  pressings, and it falls back to the old recording-level median when
  the album reports no track lengths. The recording median alone was the
  Weird Science veto above (229 s radio-edit cluster vs 368 s album cut)
  and also mis-anchored Wild Sex at 257 s (1984 single) vs the 247 s
  `Nothing to Fear` cut. Measured album-pressing lengths for the six:
  313/367/254/281/252/247 s — every one matches the scene/archive rip
  that previously got delivered or vetoed. `discover` passes the derived
  album because the album, not the song's recording row, defines the
  standard cut.
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

## MusicBrainz: `artist:` matches CREDIT TEXT, not the artist ENTITY (2026-11-13)
MB's search `artist:"X"` clause is matched against the **credit text
stored on each recording**, not against the artist ENTITY the recording is
linked to. A recording credited `Offspring` therefore does NOT match
`artist:"The Offspring"` even though it belongs to that entity — the alias
lives in the entity and the search never consults it.

`The Offspring - Self Esteem` is the case: 41 recordings come back for the
text clause, 44 with the entity union, and the 3 only the union sees are
credited `Offspring` on entity `23a03e33-a603-404e-bcbf-2c00159d7067` —
including the studio master `5a57b450-fecd-4ab5-a400-1e4b5d22dfbc`, the
257 826 ms track 8 of **Smash** (verified against pressings only the
payload lists: release `91587782`, 1994-04-19, carries that recording id at
257 826 ms). Every derivation reads the SAME recording set, so hiding the
master broke all of them at once: `musicbrainz_studio_year` 1995,
`musicbrainz_album` `'Come Out and Play'` (the 240 s single — Smash appears
nowhere in the text payload), anchor 240 s, `musicbrainz_cd_year` 2002 (a
greatest-hits CD), so the duration gate vetoed every genuine Smash rip, the
album pluck searched an album that never carried the song, and GrabbyTube
could only ever write a `fixup`.

**`_mb_recordings(artist, title, log, *, limit, inc="")` is now the only
recording-search entry point** (it replaced the per-callee `_mb_get`
queries) and it queries `recording:"T" AND (artist:"A" OR arid:"<id>" …)`:
- `_artist_ids(artist, log)` resolves the typed name to MB entities and
  keeps only those whose **name is exactly** the typed name — `exact=on`
  still returns near-matches (`The Offspring M.D` comes back for "The
  Offspring"), so the names are compared here. The union therefore adds
  only this artist's OWN recordings: it is a strict superset of the text
  query, and a differently-named band sharing the name (`Phil Collins
  Trio`) is excluded here and still excluded downstream by the credit-text
  containment filter every consumer already applies. Cached per normalized
  name in `_MB_IDS` — one `/artist` lookup per artist per process, not per
  query.
- `limit` / `inc` pass through unchanged: each consumer's own cap and
  embed decide its answer, so a shared helper must not restate them. `None`
  (MusicBrainz unreachable) stays distinguishable from `[]` (no match) —
  `_mb_confirm_title` must TRUST the verdict engine on `None` and peel on
  `[]`, and `musicbrainz_listing_artist` must skip the scrub on `None`.
- All 8 recording-query sites consume it: `musicbrainz_studio_year`,
  `musicbrainz_album`, `musicbrainz_cd_year`, both `musicbrainz_duration`
  branches, `_album_credit_names`, `_mb_confirm_title`,
  `musicbrainz_listing_artist`.

**Verified by replaying the real functions over both query forms.** Patched
`_mb_get` to the union and re-ran the derivations: Self Esteem ->
`album='Smash'`, anchor 257 s, studio_year 1994, cd_year 1994 (from
`'Come Out and Play'` / 240 / 1995 / 2002), and the six control songs with
no alias credits returned identical payloads and identical answers (Nena
1983 `Nena` 233, a-ha 1984 `Hunting High and Low` 229, Men Without Hats
1982 `Rhythm of Youth` 277, Oingo Boingo 1985 `Dead Man's Party` 313, Phil
Collins 1984 `Chinese Wall` 305 + `Philip Bailey`, Deep Blue Something 1993
`Home` 257) — each added 0 recordings, so the union changes nothing where
nothing is aliased. Same seven cases run as a battery against the shipped
code (`/tmp/t10.py`): 7/7 PASS.

**Truncation was measured, not assumed.** The union enlarges the pool while
every consumer keeps its `limit` cap, so a common title on a prolific
artist could in principle crowd the studio master out of the page. `a-ha -
Take On Me` is that worst case (109 recordings; the text query alone fills
the 100 cap): every entity-owned recording is still on page 1 under the
union (page 2 holds 9 either way) and the master sits rank 51 of 100 — and
a-ha has no alias credits, so its text pool was already complete. A future
title whose union pool exceeds the cap is the signal to page, not to drop
the union.

**What this deliberately does NOT change** (owner decision 2026-11-13,
alias scope): `musicbrainz_album`'s tally still discards Compilation-typed
release groups. `Greatest Hits` pressings DO carry the studio recording —
17–20 of them in the same payload, track 3 at 257 826 ms — and they are
invisible to the tally in a second way as well (MB credits those releases
with the alias text too). Ranking carriers by recording identity so an
unrippable original falls back to the NEXT REAL CARRIER album is deferred;
the album pluck's existing next-pressing fallback covers what scene
actually has.

**Second beneficiary, measured end to end: `The Prodigy - Mindfields`.**
The text-only query returned 25 recordings and none of the 338–340 s studio
pressings; the union returns 30 and adds five credited `Prodigy`, including
the master — 339 906 ms, first released 1997-06-30, on **73 releases**
including the 1997 `The Fat of the Land`. With the text payload alone the
album tally fell to the only bare-title candidate it could see, the VA
compilation `'The Matrix: Music from and inspired by the Motion Picture'`,
and the anchor landed on a 343–347 s sibling; with the union the tally
picks `The Fat of the Land` (the pressing count is what breaks ties) at the
true 339 s, `cd_year` 1997 instead of 1999, and the run delivers the
`REPACK-CD-FLAC-1997-AUDIO` rip at AcoustID 100 % where the old query
could only ever write a fixup. Re-ran the four derivations 2x (text) and 3x
(union): each form reproduced its own numbers exactly, so the change makes
the derivation MORE deterministic here, not less — the union supplies the
high-pressing recording the tally was always meant to rank on.

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
Part names are subject text, so the write is guarded: `sanitize_part_name`
flattens whatever `_nzb_file_name` returned (`/`, `\`, NUL/controls -> `_`,
leading/trailing dots+spaces stripped, `part` when nothing usable is left)
because a bare-file xpost posts its only yEnc part as `... yEnc 1518 (1/1)`:
`arc / "(1/1)"` hit ENOTDIR and tune() logged it as "download failed",
abandoning the release (2026-11-12, Easy Lover). `_unique_part_name` appends
`.2`, `.3`, ... on a name collision so a duplicate part never overwrites
bytes. Both are deterministic (same subject -> same name), and the track
ranking runs on the EXTRACTED tree, so these names are layout-only.

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
