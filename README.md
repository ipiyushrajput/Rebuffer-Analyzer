# TV Plus Rebuffer Analyzer (RBA)

Internal platform for the Samsung TV Plus stream quality team.

A linear HLS channel whose rebuffering ratio crosses **0.25** on the TV Plus Tizen player is
flagged as a rebuffering channel. RBA takes that channel's playback URL, analyses the master
playlist, every child playlist, cross-variant consistency, the segments and the audio and
video bitstreams, and produces a channel-wise report that names the **exact defect, the
evidence that proves it, the responsible party, and the fix** — ready to forward to the
content provider, packager, CDN, SSAI vendor, or the Samsung player team.

Every finding is a rule that fired on a measurement. There are no hypotheses in the output:
a unit test fails the build if a hedging word enters a rule, a report template, or the
analysis code.

![Realtime tab](docs/screenshots/realtime.png)

## What it does

| Tab | Purpose |
|---|---|
| **Realtime** | Paste a playback URL, analyse live. Player, sixteen live charts, live findings feed, report at any moment. Stopping files the run and clears the tab for the next one. |
| **Analysed channels** | Every finished analysis, with its verdict, findings, incidents and reports. Read back from the database, so a channel analysed before the last restart is still here; delete one and its measurements and report files go with it. |
| **Automated Batch** | One click, or one weekly firing, runs the whole pipeline on the backend: list a country, scan it against CASCADA, select the channels above threshold, analyse each one, and file the report. Playground lists every batch — manual and scheduled — with live progress, the log, the reports and re-run. |
| **Aging** | Put channels under analysis for 15 min to 24 h. Jobs run server-side and survive the browser closing and a backend restart. The stored samples draw the same charts Realtime draws live, over the last day, two days, seven days or the whole run. |
| **Bulk** | Upload CSV / XLSX / JSON of many channels. Snapshot or aging mode, with concurrency control. Consolidated ranked report plus per-channel reports. |
| **Reports** | Every report generated, searchable by channel, date, verdict and owner. |
| **Settings** | Thresholds, User-Agent profile, concurrency limits, retention, the batch configuration and its weekly firings, and the full rule catalogue. |

Realtime and Aging accept optional **Origin**, **CDN** and **SSAI (MediaTailor)** URLs
alongside the playback URL. When they are present the same checks run on each layer and a
defect is pinned to the first layer it appears on. Without them, each defect is attributed
from its own layer plus the response headers that prove it — and the report says which.

### How a finding reads

Every finding states the measurement, the root cause, the responsible party and the fix:

> **`HTTP-005` Segment listed in the playlist returns HTTP 404** — CRITICAL, CDN, `v720p@1100k`
>
> Segment `https://cdn.example/live/ch1/mid-seg3.ts` returned HTTP 404.
>
> **Root cause.** The playlist advertises a segment the edge does not hold, so the player has
> nothing to decode for that position on the timeline.
>
> **Fix.** Publish the segment to the edge before it is listed, or remove it from the playlist
> until it is present.

## Analysis in brief

- **Collectors.** Every child playlist is polled in parallel at `TARGETDURATION / 2`. The
  lowest, middle and highest video rung and every audio rendition are sampled in full; other
  rungs every Nth segment. The master is re-polled every 20 s and the ladder swept every
  5 min.
- **185 rules** across DNS, TLS, HTTP, CDN, master playlist, media playlist, sequence
  numbers, segments, video bitstream, audio, A/V sync, subtitles, SSAI and player telemetry.
  See [`docs/RULES.md`](docs/RULES.md). Each rule's severity is declared there and can be
  reassigned per deployment in Settings → Rule catalogue; the declaration is never edited, so
  a report always states both what the rule reports and what the catalogue declares.
- **A boundary is only judged when it was observed.** The timestamp continuity rules compare
  where one segment ends against where the **next** begins, so they run only on a pair whose
  media sequence numbers are adjacent. A rung sampled every Nth segment never produces an
  adjacent pair, and a rung sampled in full misses one whenever a segment rolls out of the
  live window between polls; measured across such a skip, the missing segment's own duration
  reads as a gap of exactly that length. `INFO-005` states each boundary that could not be
  checked and how many segments were skipped, so a polling gap is reported as itself rather
  than as a defect in the stream.
- **A protected channel is analysed like a clear one.** A Widevine playback URL goes into
  Realtime, Aging, Bulk or an automated batch exactly as a clear one does, and nothing about
  DRM is asked per channel. The backend reads what the ladder declares, takes the key
  identifier from each track's own `tenc` box, obtains the content key from the KeyOS key
  server over CPIX, and decrypts the CENC sample payloads in process before the bitstream
  rules read them — so the decode-error, SPS-consistency, ADTS and keyframe checks run on the
  packager's own bitstream. The player gets its licence through `POST /api/drm/license`,
  which relays the challenge from this host, because a browser has no route to the licence
  server and it sends no CORS headers. A rendition whose key could not be obtained is
  reported as one through `INFO-001` and in the report's protection table, with the reason —
  never analysed as though the bitstream checks had passed. The deployment is configured once
  in **Settings → DRM**; see [DRM](#drm). The **preview** is the one part that needs the page
  itself to be a secure context, because Encrypted Media Extensions are only available on
  one: `http://localhost`, a Chrome origin-trust flag, or HTTPS. Nothing about the stream or
  the licence server has to be HTTPS, and the analysis is unaffected either way — the
  preview states which origin is the problem rather than failing with a player error nobody
  can read, and the licence lifecycle goes into the run's event log.
- **An evidence bundle carries the stream.** A run recording evidence holds the sampled
  segment bytes and streams them into the archive beside `result.json` and the playlist
  snapshots — `segments/` for a clear rung, `encrypted/` for a protected one as the CDN
  served it, and, where a deployment turns it on in Settings → DRM, `decrypted/` with each
  segment's initialisation segment in front of it and a manifest naming every file's source
  URI, media sequence number and rendition. The window is bounded and the bytes around an
  incident are kept longest; the bundle's `README.txt` states how many segments it holds, how
  many were dropped, and which reason applies when it holds no decrypted media. No content
  key, private key or credential is ever written to one.
- **A demuxed ladder is measured as one.** Every TV Plus CMAF channel carries its audio in a
  rendition of its own, and its video segments hold no audio track — which is the format
  working, not a defect. The ladder's packaging is read from the master and carried to every
  check, so `AUD-003` ("Muxed segment carries video and no audio elementary stream") asks
  whether the rung has anywhere else to put its audio before it fires, and still fires on a
  muxed rung that is genuinely silent. A/V skew on such a ladder is measured across the two
  renditions: video and audio segments are paired by absolute media sequence number, verified
  to overlap on the timeline by at least half the video segment's own length, and every skew
  finding names both segments and how they were matched. A video segment whose own audio
  segment was never sampled is paired with nothing rather than with the one beside it. A video segment whose audio is not published yet is held rather than measured
  against the wrong one, and a video range no audio segment covers is `AUD-006`.
- **The analyzer identifies as a Samsung TV.** Every manifest and segment request carries a
  Tizen `User-Agent`, because a CDN and a packager both answer per User-Agent and a run made
  as a desktop browser measures a different response than the fleet sees. Eleven Tizen
  releases are declared, from 2.4 to 10.0, plus one desktop string for comparison; the
  default is **Tizen 10.0 (2026)**, set in **Settings → General** and overridable per
  analysis in every form. The preview player fetches through `/api/proxy`, so its requests
  carry the same string. The profile and the full User-Agent are recorded in the job's
  options, stated in the report appendix and written into the evidence bundle's `README.txt`,
  so a run can be replayed under the conditions it was measured under. The channel
  catalogue, CASCADA and the licence server keep their own.
- **Virtual Player Buffer.** Plus Player's own buffering configuration modelled against the
  delivery timings actually measured, so Aging and Bulk produce a rebuffering ratio with no
  player attached, and Realtime shows the model next to the real hls.js buffer. The profile
  comes from the tallest rung the ladder offers: a ladder topping out at 1080p buffers as
  FHD, one reaching 2160p as UHD. Playback starts at the 33% multiqueue watermark, underruns
  at the 1% one, and resumes at 66%. Three sensitivity modes: `STRICT`, `NORMAL`,
  `OUTAGE_ONLY`.
- **Correlation.** Every stall — real or simulated — is matched to the events measured in
  `[stall_start − 2 × TARGETDURATION, stall_end]` on its rung and rendered as one chain:
  `CDN stale playlist 720p 12:03:02–12:03:20 (MED-004) → segment 48213 listed late → player
  stall 12:03:14, 4.1 s`.
- **Incidents** open only after a condition persists `incident_open_s` and close after
  `incident_clear_s` of clean delivery, so reports list the periods a viewer experienced
  rather than raw event spam.
- **Verdict.** One primary root cause ranked by severity × rebuffer impact × occurrence ×
  stall correlation, the contributing defects, and a 0–100 Rebuffer Risk Score whose formula
  is printed in the report appendix.

## Reports

![Report verdict](docs/screenshots/report-verdict.png)

One Jinja2 template renders the HTML that Playwright prints to PDF, so the two never
diverge. The HTML is self-contained — inline CSS, inlined chart library, base64 logo — so it
can be emailed as a single attachment.

Each report carries the executive verdict, a **ready-to-send escalation block per
responsible party**, the findings grouped by owner with expandable evidence, the incident and
stall-correlation timeline, the charts, the ladder audit, the checks that passed, and an
appendix with the full URLs, redirect chains, layer attribution, manifest snapshots around
each incident, the thresholds used, the risk-score formula and a glossary.

![Report findings](docs/screenshots/report-findings.png)

## Quick start

Python 3.11+ and Node 20+ are the only prerequisites. ffmpeg is optional — without it the
decode-error and quality detectors do not run and every other check does. Full instructions,
including the `.env` file and the troubleshooting list, are in
[`docs/SETUP.md`](docs/SETUP.md).

**Linux and macOS**

```bash
make install                            # backend venv + frontend node_modules
cp backend/.env.example backend/.env    # fill in DB_*, or set DB_ENGINE=sqlite
make dev                                # backend on :8010, frontend on :5173
```

**Windows**

```powershell
.\deploy\windows\setup.cmd -Dev         # venv, backend, Playwright, node_modules, .env
.\deploy\windows\rba.cmd dev            # backend on :8010, frontend on :5173
```

`rba.cmd` is the Makefile's counterpart — `dev`, `serve`, `build`, `test`, `lint`, `rules`,
`analyse`, `clean`, `help`. `serve` builds the bundle and serves it on :8080 next to the
backend, which is the Windows stand-in for nginx. Neither script changes the machine's
execution policy, and both refuse port 8001.

Headless, without the UI:

```bash
cd backend
.venv/bin/python -m app.cli analyse "https://cdn.example/live/ch1/master.m3u8" \
    --duration 5m --html out.html --pdf out.pdf
.venv/bin/python -m app.cli rules --markdown > ../docs/RULES.md   # or: make rules
```

```powershell
.\deploy\windows\rba.cmd analyse "https://cdn.example/live/ch1/master.m3u8" --duration 5m --html out.html
```

When a run measures no segment, `diagnose` says why. It reports the resolved ladder and,
per rendition, the HTTP status, how many segments the playlist listed, how many were
sampled, and any handler failure with its traceback:

```bash
.venv/bin/python -m app.cli diagnose "https://cdn.example/live/ch1/master.m3u8"
```

```powershell
.\deploy\windows\rba.cmd diagnose "https://cdn.example/live/ch1/master.m3u8"
```

`analyse` exits `0` when no stream-side defect was found and `2` when one was, so it drops
into a monitoring cron — or a Windows scheduled task — without further glue.

With Docker, on either platform:

```bash
make up             # backend :8010, frontend :8080
```

## Deployment on 107.109.131.68

| Service | Port |
|---|---|
| Backend (uvicorn) | **8010** |
| Frontend (nginx, reverse-proxies `/api` and `/ws`) | **8080** |
| *Reserved — never used by RBA* | 8001 (existing Metanalyser backend) |

Check the host before binding:

```bash
ss -ltnp | grep -E ':(8010|8080)\b'
```

If either port is taken, take the next free one and record it here and in `backend/.env`.
`deploy/install.sh` performs this check itself and stops rather than binding over something.

Frontend build variables:

```
VITE_API_BASE=http://107.109.131.68:8080/api
VITE_WS_BASE=ws://107.109.131.68:8080/ws
```

Without Docker:

```bash
sudo deploy/install.sh      # venv, migrations, frontend build, systemd unit, nginx site
journalctl -u rba-backend -f
```

The systemd unit runs a **single worker on purpose**: jobs are held in process and resumed
from the database on restart, so a second worker would run each aging job twice. It caps
memory at 6 GB, which holds the 20 concurrent aging jobs the analyzer is sized for with
room to spare — the figure the load test in `backend/tests/test_load.py` measures against.

## Database

**Everything RBA records lives in your SQL server** — jobs, findings, incidents, samples,
playlist snapshots, settings, and the rendered reports themselves. The analyzer host keeps
no state of its own, so a second instance serves a report it did not render and nothing is
lost when a container is replaced.

Point it at your server in `backend/.env`:

```
DB_ENGINE=mysql          # mysql | mariadb | postgres | sqlite
DB_HOST=10.0.0.5
DB_PORT=3306
DB_USER=rba
DB_PASSWORD=…
DB_NAME=rba
```

On start RBA creates the `rba` database and its tables, then reports the outcome at
`/api/health` without revealing the host or the user. It never touches another
application's tables. If the account cannot create databases, point `DB_NAME` at an
existing one and set `DB_TABLE_PREFIX=rba_`; every RBA table is then created inside it
under that prefix.

`DB_ENGINE=sqlite` needs nothing else and keeps a single file in `RBA_DATA_DIR` — useful
for a laptop, not for the deployment.

Credentials live only in `backend/.env`, which is git-ignored. `backend/.env.example`
carries the variable names. A `secret-scan` hook aborts any commit that would carry a value
from `.env` into the repository.

**Schema**: `channels`, `jobs`, `bulk_items`, `findings`, `incidents`, `samples_playlist`,
`samples_segment`, `samples_player`, `virtual_buffer`, `playlist_snapshots`, `reports`,
`settings`, `cascada_samples`, `batches`, `batch_items`, `batch_logs`, `batch_schedules`,
each sample table indexed on `(job_id, variant, ts)` and on `(job_id, ts)`.

Every column a listing orders by is indexed, and no listing sorts the rows themselves. MySQL's
filesort packs each selected column into `sort_buffer_size`, so ordering `jobs` — two JSON
columns and five TEXT ones per row — by `created_at` fails with error 1038, "Out of sort
memory", once a deployment has some history. `app/db/paging.py` sorts a projection of the
primary key and fetches the rows by key, which costs one extra round trip and nothing else.

A retention job purges raw samples past the configured window (30 days by default) while
keeping findings, incidents and reports; snapshots pinned around an incident survive the
purge.

**Reports** are stored as bytes in `reports.content` (`LONGBLOB` on MySQL) and streamed from
there. `RBA_REPORT_STORAGE` decides where they go:

| Value | Behaviour |
|---|---|
| `database` | The bytes go in the table and nothing is written to the host. **The default.** |
| `both` | The database, plus a mirrored copy in `RBA_REPORTS_DIR`. |
| `disk` | Files only, as builds before this behaved. |

An HTML report is about 1 MB and a PDF about 250 KB. Bulk archives are assembled in memory
and stored the same way, so a batch of 50 channels produces one row of roughly 20 MB —
raise MySQL's `max_allowed_packet` past that if you run large batches. Evidence archives are
built in memory from the stored rows and streamed, never written out.

Applying the schema to an existing database:

```bash
cd backend && .venv/bin/alembic upgrade head
```

## API

```
POST   /api/realtime/sessions              DELETE /api/realtime/sessions/{id}
WS     /ws/realtime/{id}                   server→client analysis, client→server telemetry
GET    /api/realtime/sessions/{id}/result  interim result while the session runs
GET    /api/realtime/sessions/{id}/manifests
GET    /api/realtime/sessions/{id}/snapshots?variant=&at=   time travel with a diff
POST   /api/realtime/sessions/{id}/report?format=html|pdf

POST   /api/aging/jobs      GET /api/aging/jobs      GET|DELETE /api/aging/jobs/{id}
GET    /api/aging/jobs/{id}/result           GET /api/aging/jobs/{id}/report.{html|pdf}
GET    /api/aging/jobs/{id}/samples?from=&to=&max_points=   stored samples for the charts

POST   /api/bulk/jobs       GET /api/bulk/jobs/{id}  POST /api/bulk/validate
GET    /api/bulk/jobs/{id}/report.{html|pdf}         GET /api/bulk/jobs/{id}/reports.zip
GET    /api/bulk/template.{csv|xlsx|json}

GET    /api/catalogue/countries              every selectable country and environment
GET    /api/catalogue/channels?country=&env=&page=&today=    one page of the TV Plus list

GET|PUT|DELETE /api/cascada/session   ·  POST /api/cascada/session/validate
GET    /api/cascada/channel?service_id=&channel_name=&country=&refresh=
GET    /api/cascada/channel/report.{csv|xlsx}
POST   /api/cascada/scans   ·  GET|DELETE /api/cascada/scans/{id}
GET    /api/cascada/scans/{id}/report.{csv|xlsx}

GET|PUT /api/batch/settings   ·  GET|PUT /api/batch/schedules  ·  DELETE /api/batch/schedules/{country}
GET    /api/batch/estimate?country=          channels and runtime, before a batch is started
POST   /api/batch/batches   ·  GET /api/batch/batches  ·  GET|DELETE /api/batch/batches/{id}
POST   /api/batch/batches/{id}/rerun         GET /api/batch/batches/{id}/log?download=
GET    /api/batch/batches/{id}/report.{csv|xlsx}      GET /api/batch/columns

GET    /api/reports  ·  GET|DELETE /api/reports/{id}  ·  GET /api/reports/{id}/download
GET    /api/jobs/{id}/evidence.zip   ·  GET /api/jobs/{id}/snapshots?variant=&at=
GET    /api/proxy?u=<urlencoded>
GET|PUT /api/settings   ·  GET /api/settings/rules   ·  GET /api/health
PUT    /api/settings/rules/{rule_id}         reassign one rule's severity; null restores it
```

Interactive documentation is served at `/api/docs`.

### All channels

The TV Plus channel list for one country and environment, fetched by the backend rather than
the browser: the catalogue sends no CORS headers, and the country-to-`dbconnect` mapping
belongs on the server. `today` is the date the operator pressed Search, and every later page
repeats it so paging stays inside one search. Playback URLs arrive with the
`|COMPONENT=HLS` routing marker removed, in both its raw and percent-encoded spellings.

Each row opens Realtime or Aging with the channel filled in — name, service ID and country
in the channel name, and the clean playback URL. Nothing starts on its own, and every field
stays editable.

The catalogue lists channels with no `CNTN_URI`; on a staging country that can be half the
page. Those channels are shown with the rest, stated as carrying no playback URL, and their
two actions are disabled — a channel that exists but cannot be analysed is a fact worth
seeing, not a row to drop. Column positions are read from the response's own `metaData`
block rather than from the order the fields arrive in.

Countries are grouped by the data set they read: **Group A** (AU, BR, CA, IN, KR, MX, NZ,
TH, US, PH, SG) and **Group B** (AT, BE, DE, DK, FI, FR, IE, IT, LU, NL, NO, PT, ES, SE, CH,
GB, EG, SA, AE). Adding a country or moving an environment is one edit in
`backend/app/tvplus/catalogue.py`.

### CASCADA Data

Every other tab measures a channel from the outside. This one reads what real televisions
reported: CASCADA publishes a per-minute `rebuffering_ratio` per channel, so the field signal
picks the targets and the rest of the product explains the cause.

The channel list is the same catalogue All channels shows, production only. The rebuffering
figures are fetched by the backend, never the browser — the API needs a session cookie and
sends no CORS headers.

**The window.** `from` is 00:00:00 UTC seven days before today, `to` is the minute of the
call. CASCADA returns that window tagged `date_category=origin` and the seven days before it
tagged `comparison`. Every figure the product states — the average, the maximum, the minutes
above threshold, the red row, both reports — comes from the `origin` rows. The `comparison`
rows draw the previous-week overlay and the week-on-week chip, and nothing else: mixing them
would average a fortnight into a number labelled as this week. `limit` is computed from the
window rather than fixed, and a response shorter than the window asked for is reported as
short rather than averaged as if it were whole.

**The threshold is a percentage.** `unit.rebuffering_ratio` is `%`, so a value of 0.159 means
0.159% of viewing time. That is not the fraction `rebuffer_ratio_threshold` holds, where 0.25
means 25% — the two are a hundredfold apart, so the field metric carries its own threshold,
`cascada_rebuffering_threshold_pct`, and one helper applies it.

**A channel qualifies on its average.** One minute spiking above the threshold does not make
a rebuffering channel; a week averaging above it does. A minute CASCADA reported nothing for
is a gap, never a zero.

**The scan** walks every channel in a country, across every page, a few at a time, with
progress, cancellation and a named list of the channels that failed. Each result is stored in
`cascada_samples` keyed by service ID and window, so a finished scan survives a restart, a
reopened panel costs nothing, and a second analyzer instance serves the same country report.
A stored window is served without a session — it was already measured — while a fresh
measurement needs one.

From there the country report downloads as CSV or XLSX, listing only the channels above
threshold worst first, and **Bulk analyse rebuffering channels** hands that set to the
existing Bulk analysis tab as the file it already parses. Above-threshold channels the
catalogue lists with no playback URL stay in the report and are counted out of the hand-off
with the reason.

Both reports are written as a table: the column names sit on row 1 and each record runs left
to right beneath them, which is what a spreadsheet sorts, filters and pivots. The window, the
threshold and the scope follow the data in a CSV and sit on an `about` sheet in a workbook —
an average means nothing without them, but they must not push the header row down the sheet.
The country report carries each channel's `playback_url` from the catalogue, so it is enough
on its own to hand a channel to whoever has to analyse it.

**The session.** CASCADA sits behind the corporate identity provider and that provider
requires MFA, so the analyzer cannot sign itself in. It carries an operator's session
instead, pasted in Settings → CASCADA. A browser cannot hand its own session over: those
cookies belong to `cascada.samsungcloud.tv`, so this application never receives them and
cannot read them, and Django marks `sessionid` HttpOnly. Copying the Cookie header out of
devtools is the route, and the panel takes the whole header. What is stored never comes back
to the browser — the panel shows the last four characters, the source, and when the session
was last proven to work. A host may instead pin its own identity with `CASCADA_SESSIONID` in
the git-ignored `backend/.env`.

### Automated Batch

Everything above the analysis engine used to be manual: open CASCADA Data, pick a country,
run a scan, read the list, hand the channels to Bulk or Aging, and stay for the result. A
batch runs that whole sequence on the backend, so nothing depends on a browser being open.

**The pipeline.** List the country from the same catalogue All channels reads → scan every
listed channel against CASCADA → select the channels whose **average** is above threshold →
analyse each selected channel for the configured duration, four at a time → render the
report. Each stage is a row: `batches` carries the counters and the settings snapshot,
`batch_items` carries one row per selected channel, and `batch_logs` carries every step. So
progress survives a reload, a different browser and a restarted process, and a batch left
running by a restart is re-entered at its first incomplete phase rather than left hanging.

The scan stage is literally the scan the CASCADA Data tab runs — `cascada.start_scan`, driven
by the batch, not a second copy of the loop. That is what gives it the country's stored windows
in one query, one HTTP client shared across every channel, and a counter that rises as each
channel lands. Playground reads that counter, so a country of 268 channels shows
`Scanning 84 / 268` while it works rather than standing at zero until the last channel returns.

**The settings are frozen at the start.** A batch snapshots the configuration it begins with,
including the CASCADA threshold and window, so an edit in Settings never disturbs a running
batch and the report states the values its figures were judged against.

**The weekly firing.** A `batch_schedules` row per country — weekday, time of day in UTC, and
`last_success_at` — read by a loop that wakes each minute. GB fires Monday 02:00 UTC by
default. A firing is skipped, with the reason written to the row and the log, when a batch for
that country is still running or fewer than seven days have passed since its last success. The
schedule is a row rather than a timer so a deployment that restarts on a Sunday evening does
not silently miss Monday.

**Aging follows a scheduled batch only.** Its above-threshold channels then age for the
following week, five at a time, worst average first; the rest are recorded as skipped with the
reason. When the next scheduled batch for that country starts, the previous week's aging runs
are cancelled before the new ones begin, so they cannot pile up. A manual batch starts no
aging run.

**The correlation.** The next week's report says what the aging run captured **at the times
the rebuffering ratio was actually high**: consecutive minutes above threshold make a spike
window, aging events are matched into it within a tolerance, and a window that caught nothing
says so rather than being left out. Both sides are normalised to UTC before they are compared.

**The runtime cap** is `max_runtime_minutes ÷ analysis_duration_minutes × analysis_concurrency`
— 480 channels at the defaults (four in parallel, two minutes each, four hours). No TV Plus
country reaches that: the largest lists around 255 channels, which is 128 minutes even if
every one of them is above threshold.

**The report** is written as a table, the same way the CASCADA reports are: `Channel Name`,
`Service ID`, `Country`, `Avg Rebuffering Ratio (7 days)` labelled from the window actually
used, `Week [start date - end date]`, `Analysis Summary`. A workbook carries the
above-threshold channels, the channels that failed with the reason, the spike-to-aging
correlation, and an `about` sheet with the window, the threshold and the settings the batch
ran with. Reports live in `reports.content` like every other report, so a second analyzer
instance serves one it did not render.

**Playground** lists every batch, manual and scheduled, with its country, window, progress,
status and the actions: download XLSX or CSV, open the per-channel detail, read or download
the log, cancel after the channel in flight, and re-run with today's settings. It polls only
while a batch is running.

The batch configuration and the weekly firings are edited in **Settings → Automated batches**.
They save through their own endpoints rather than the threshold save bar, because a schedule
is a row a running loop reads every minute and a half-edited one would fire.

### DRM

Protected channels need no per-channel input. What a deployment configures, once, is in
**Settings → DRM**:

| Setting | What it is |
|---|---|
| Widevine licence URL | The licence server the player acquires its licence from. The browser never reaches it; `POST /api/drm/license` relays the challenge from this host. |
| CPIX endpoint | The key server content keys are requested from. Empty uses the KeyOS v4 endpoint. |
| Client certificate | A path on the analyzer host, or an HTTPS URL. The CPIX request is signed with it and the keys come back encrypted to it. |
| Client private key | A path on the analyzer host, or an HTTPS URL. |
| Key server certificate | The key server's own certificate. A response signed with a different key is reported. |
| Content identifier | What a CPIX document names the content as. One value for the deployment. |

The same values can be set in `backend/.env` (`DRM_LICENSE_URL`, `CPIX_*`) to pin them to the
host. **The three CPIX values are a location, never key material**: no certificate, private
key or content key belongs in `.env.example`, in git, or in a log. Keep the private key
behind authentication — whoever can read it can read every content key the deployment
obtains. What the settings endpoint returns is whether each one is set, never what it is.

`cenc` (AES-CTR, `SAMPLE-AES-CTR`) is decrypted. `cbcs` is a different cipher and is named
rather than attempted: a rendition using it is reported as unread with its scheme stated.
A ladder that leaves one rendition in the clear is analysed without a key for that rung.

### Bulk input

Required: `channel_name`, `playback_url`. Optional: `channel_id`, `country`,
`content_provider`, `cdn`, `origin_url`, `cdn_url`, `ssai_url`. Column names are matched
case-insensitively through an alias table — `url`, `hls_url`, `m3u8`, `Channel`, `Playback
URL` and similar all resolve — and every row is validated before the job starts, with
row-level errors shown for correction.

![Bulk tab](docs/screenshots/bulk.png)

## Interface

The application follows one design system, **Signal Clarity**, and the report templates
follow it too, so a PDF sent to a vendor looks like the screen the operator read it on.

- **Structure.** A collapsible dark rail carries the sections and the host's health.
  Every screen opens with a page header — what this is, what state it is in, who is
  operating it — then a row of measurements, then the evidence. Opening a finding replaces
  the body with its investigation: the causal chain, the request evidence, the failing
  manifest line, and a decision card naming the root cause, the responsible party and the
  required fix, with the escalation text one click away.
- **Colour.** Samsung blue `#1428A0`, violet `#7B2CBF`, TV Plus pink `#FF2D55`, and one
  green `#12864C` for a clean result, on white. The three-stop gradient is reserved for the
  executive verdict and the report header. Severity runs green → blue → violet → pink, and
  colour is never the only signal — every severity carries its own word, and findings carry
  a coloured rail on the card edge.
- **Type.** One UI Sans → SamsungOne → Inter → Arial for the interface. JetBrains Mono →
  Roboto Mono for machine evidence only: manifests, URLs, timestamps, HTTP information,
  identifiers and measured numbers.

Tokens live in `frontend/tailwind.config.js`, component classes in `frontend/src/theme/index.css`,
and primitives in `frontend/src/components/ui/`.

## Documentation

- [`docs/SETUP.md`](docs/SETUP.md) — setup and run instructions for Linux, macOS and
  Windows, the `.env` variables, and what to do when something does not start.
- [`docs/RULES.md`](docs/RULES.md) — the full rule catalogue, generated from the registry by
  `make rules`. CI fails if it drifts from the code.
- [`docs/REFERENCE_TOOLS.md`](docs/REFERENCE_TOOLS.md) — coverage matrix against
  HLSAnalyzer, Qosifire, the Dolby Stream Validator, the Akamai Stream Validator and
  THEOplayer Inspect Stream, with out-of-scope rows carrying their reason. A test fails if
  the matrix names a rule that no longer exists.
- [`CLAUDE.md`](CLAUDE.md) — conventions, commands and the project's non-negotiables.

## Development

```bash
make test      # pytest + vitest
make lint      # ruff, ruff format, mypy, eslint, tsc
make rules     # regenerate docs/RULES.md
```

Tests run against a **fault-injecting fixture origin**: a pure-Python TS muxer and playlist
renderer that place a chosen fault exactly where a rule expects it — a PTS gap of a given
size, an AAC sample-rate change at a given segment, a segment that starts on a non-IDR slice
— served over an HTTP origin that can add redirects, required query tokens, 404s, delays and
wrong content types. No ffmpeg is needed to run the suite.

Every rule has a positive and a negative case, so a rule that stops firing, or starts firing
on a clean stream, fails the build.

## Notes on measurement

Player metrics shown in the Realtime tab are **measured from the analyzer host**. They
reflect the analyzer's network path, not a TV's; the UI labels them so everywhere they
appear. The Virtual Player Buffer is what produces a rebuffering ratio for Aging and Bulk.

The Virtual Player Buffer follows the buffering configuration in section 3 of the Samsung TV
Plus player document: 15 s total for both FHD and UHD, with the multiqueue low watermark at
1%, the startup high watermark at 33% and the resume/seek high watermark at 66%. That
reproduces the 5 s startup and 10 s resume figures the same table states. The document also
gives byte caps — 3 MB for FHD, 60 MB for UHD — and these are implemented, configurable and
**off by default** (`vpb_apply_byte_caps`). Applied literally the FHD cap holds 3.2 s of
7.5 Mbit/s media, less than a single 6 s segment, so it calls an on-time 1080p channel a
continuous rebuffer. Section 2 of the same document lists `OutputMgr` as a separate queue
holding downloaded segments, so the byte figures size the decoder-side multiqueue rather than
the buffer that governs rebuffering. The switch turns them on once the player team confirms
which queue they describe.

TLS verification is disabled on every outbound fetch so a broken chain never blocks analysis.
The certificate chain is still inspected and reported — disabling verification hides nothing.

URLs are used verbatim: every query parameter survives, redirects are followed one hop at a
time so each status, header and timing is recorded, and child URIs resolve against the final
post-redirect URL. A `|COMPONENT=HLS` suffix is stripped before the request.
