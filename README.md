# TV Plus Rebuffer Analyzer (RBA)

Internal platform for the Samsung TV Plus stream quality team.

A linear HLS channel whose rebuffering ratio crosses **0.25** on the TV Plus Tizen player is
flagged as a rebuffering channel. RBA takes that channel's playback URL, analyses the master
playlist, every child playlist, cross-variant consistency, the segments and the audio and
video bitstreams, and produces a channel-wise report that names the **exact defect, the
evidence that proves it, the responsible party, and the fix** — ready to forward to the
content provider, packager, CDN, SSAI vendor, or the Samsung player team.

Every finding is a rule that fired on a measurement. There are no hypotheses in the output.

## What it does

| Tab | Purpose |
|---|---|
| **Realtime** | Paste a playback URL, analyse live. Player, live charts, live findings feed, report at any moment. |
| **Aging** | Put channels under analysis for 15 min to 24 h. Jobs run server-side and survive the browser closing and a backend restart. |
| **Bulk** | Upload CSV / XLSX / JSON of many channels. Snapshot or aging mode, with concurrency control. Consolidated ranked report plus per-channel reports. |
| **Reports** | Every report generated, searchable by channel, date, verdict and owner. |
| **Settings** | Thresholds, User-Agent profile, concurrency limits, retention. |

Realtime and Aging accept optional **Origin**, **CDN** and **SSAI (MediaTailor)** URLs
alongside the playback URL. When they are present the same checks run on each layer and a
defect is pinned to the first layer it appears on.

## Quick start

```bash
make install        # backend venv + frontend node_modules
cp backend/.env.example backend/.env    # fill in DB_* from the deployment
make dev            # backend on :8010, frontend on :5173
```

Headless, without the UI:

```bash
cd backend
.venv/bin/python -m app.cli analyse "https://cdn.example/live/ch1/master.m3u8" \
    --duration 5m --html out.html
.venv/bin/python -m app.cli rules --markdown > ../docs/RULES.md
```

With Docker:

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

Frontend build variables:

```
VITE_API_BASE=http://107.109.131.68:8080/api
VITE_WS_BASE=ws://107.109.131.68:8080/ws
```

A systemd alternative to Docker lives in [`deploy/`](deploy/).

## Database

RBA uses the database server configured in the `ipiyushrajput/Transcoder` deployment, in a
**dedicated `rba` database**. It never touches Transcoder's tables. If the DB user cannot
create a database, set `DB_TABLE_PREFIX=rba_` and RBA creates its tables inside the existing
database under that prefix instead. `/api/health` reports database status without revealing
the host or the user.

Credentials live only in `backend/.env`, which is git-ignored. `backend/.env.example` carries
the variable names with empty values.

## Documentation

- [`docs/RULES.md`](docs/RULES.md) — the full rule catalogue, generated from the registry.
- [`docs/REFERENCE_TOOLS.md`](docs/REFERENCE_TOOLS.md) — coverage matrix against HLSAnalyzer,
  Qosifire, the Dolby Stream Validator, the Akamai Stream Validator and THEOplayer Inspect
  Stream.
- [`CLAUDE.md`](CLAUDE.md) — conventions, commands and the project's non-negotiables.

## Notes on measurement

Player metrics shown in the Realtime tab are **measured from the analyzer host**. They
reflect the analyzer's network path, not a TV's. The Virtual Player Buffer models a
Tizen-like player against the measured delivery timings, so Aging and Bulk produce a
rebuffering ratio without a player attached.

TLS verification is disabled on every outbound fetch so a broken chain never blocks
analysis. The certificate chain is still inspected and reported.
