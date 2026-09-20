# RBA — TV Plus Rebuffer Analyzer — project memory

Internal platform for the Samsung TV Plus stream quality team. It analyses linear HLS
channels end to end and produces a channel-wise report naming the exact defect, the
evidence, the responsible party, and the fix.

## Non-negotiables

1. **TLS verification is disabled on every outbound fetch** (`verify=False` in httpx).
   The urllib3 `InsecureRequestWarning` is suppressed once, in `app/net/fetcher.py`.
   `app/net/tls_inspect.py` still inspects and reports the certificate chain — disabling
   verification must never hide a finding.
2. **URLs are used verbatim.** Every query parameter (tokens, `ads.*`, session IDs) is
   preserved. Redirects are followed **manually** so each hop, status, header and timing
   is recorded. Child and segment URIs resolve against the **final post-redirect URL**,
   never the original. A `|COMPONENT=HLS` suffix is stripped before the request.
   See `app/net/fetcher.py` and `app/hls/uri.py`.
3. **Deterministic verdicts only.** No "may be", "might", "possibly", "likely", "appears
   to". Every finding is a rule that fired on measured evidence and states: exact error,
   exact evidence, root cause, owner, fix. `tests/test_forbidden_words.py` fails the build
   if hedging words enter rule text or report templates.
4. **No AI attribution in git.** No `Co-Authored-By: Claude`, no "Generated with Claude
   Code", no session links — in commits, PR bodies, README, or code comments. Enforced by
   `.claude/settings.json`, `.githooks/commit-msg`, and a post-push `git log` check.
5. **Secrets never enter git.** DB credentials live only in `backend/.env` (git-ignored).
   `backend/.env.example` carries variable names with empty values.

## Commands

```
make dev          # backend (uvicorn --reload) + frontend (vite) together
make backend      # backend only, port from RBA_PORT (default 8010)
make frontend     # frontend only, port 5173
make test         # pytest + vitest
make lint         # ruff + mypy + eslint + tsc --noEmit
make build        # frontend production build + backend wheel
make up / make down   # docker compose
make rules        # regenerate docs/RULES.md from the rule registry
```

On Windows the same tasks run through PowerShell, since there is no `make`:

```
deploy\windows\setup.cmd -Dev       # venv, backend, Playwright, node_modules, .env
deploy\windows\rba.cmd dev          # backend :8010 + Vite dev server :5173
deploy\windows\rba.cmd serve        # build the bundle, serve it on :8080 with the backend
deploy\windows\rba.cmd test|lint|build|rules|analyse|clean|help
```

`rba.cmd` mirrors the Makefile target for target; add a target to one and add it to the
other. Both installers refuse port 8001, and `tests/test_deploy_scripts.py` fails the build
if either stops doing so. Setup for every platform is documented in `docs/SETUP.md`.

Backend CLI for headless use:

```
python -m app.cli analyse <url> --duration 5m --html out.html
python -m app.cli diagnose <url>              # why sampling measured what it measured
python -m app.cli rules --markdown            # rule catalogue
```

## Layout

```
backend/app/
  api/        REST routers (realtime, catalogue, channels, aging, bulk, batch, reports,
              proxy, settings, health)
  ws/         WebSocket hub + typed message schemas
  jobs/       job manager, persistence, resume-on-restart
  net/        fetcher (manual redirects + timing split), dns, tls_inspect
  hls/        m3u8 master/media models, URI resolution, SCTE-35
  media/      ts, fmp4, h264_sps, hevc_sps, adts, ffprobe
  analysis/   collectors/, rules/, vpb.py, correlate.py, attribution.py, verdict.py
  reports/    Jinja2 templates + HTML/PDF renderers + escalation blocks
  bulk/       csv/xlsx/json parsing with column alias mapping
  db/         SQLAlchemy models, repository, session factory
  tvplus/     the live channel catalogue: country table, dbconnect mapping, row parser
  cascada/    the field rebuffering metric: auth, client, series, store, scan, exports
  batch/      the automated pipeline: settings, store, runner, scheduler, aging,
              correlate, summary, reporting, exports
frontend/src/
  tabs/       Realtime, AllChannels (the TV Plus catalogue), CascadaData (measured
              rebuffering), AutomatedBatch (Start + Playground), Channels (analysed
              channels), Aging, Bulk, Reports, Settings
  components/ Player, charts/, FindingCard, ManifestViewer, SequenceLadder
```

## Conventions

- Python 3.11+, `ruff` + `mypy --strict` on `app/`, `pytest` with `pytest-asyncio`.
- Every rule is declared in `app/analysis/rules/` and registered in the registry at import
  time. A rule carries `id`, `layer`, `severity`, `owner`, `title`, `root_cause`, `fix`,
  `rebuffer_impact`. Text is written in present-tense definite statements.
- **The declared severity is the product's; the override is the deployment's.** A team
  reassigns a rule in Settings → Rule catalogue rather than by editing `catalogue.py`.
  Overrides live under the `rule_severity` settings key, are applied once in
  `Rule.raise_finding` so every detector picks them up, and outrank a severity a detector
  computed for itself. `docs/RULES.md` and the CLI always print the declaration, the API
  prints both, and a report lists every reassignment in force in its appendix beside the
  thresholds — a reader of an escalation has to be able to see that a severity was changed.
  A stored override for a rule the catalogue no longer declares is dropped on load, never
  carried onto whichever rule takes that id next.
- **A pair rule runs only on a pair that was observed.** `check_segment_pair` compares the
  last *sampled* segment with the current one, and those are not always adjacent: a rung
  sampled every Nth segment never produces an adjacent pair, and a full rung misses one
  whenever a segment rolls out of the live window between polls. Every rule that asserts
  where one segment ends against where the **next** begins — the PTS gap, overlap, reset and
  rollover-split checks — is gated on `current.msn == previous.msn + 1`, because across a
  skip the missing segment's own duration reads as a gap of exactly that length. The skipped
  boundary is reported as `INFO-005` with the count, never dropped. A config-change check is
  not gated: a change observed across a skip is still a change.
- Thresholds are never inlined. They live in `app/config.py` (`Thresholds`), are editable
  in the Settings tab, and are persisted in the DB.
- **The Virtual Player Buffer models Plus Player, not a generic player.** `app/analysis/vpb.py`
  carries the buffering configuration from section 3 of the TV Plus player document: a
  profile picked from the tallest rung in the ladder (1080p → FHD, 2160p → UHD), a 15 s time
  cap, and the multiqueue watermarks 1% low / 33% startup / 66% resume. The documented byte
  caps (3 MB FHD, 60 MB UHD) are implemented behind `vpb_apply_byte_caps`, off by default
  because applied literally they call an on-time 1080p channel a continuous rebuffer; section
  2 lists `OutputMgr` as the separate download queue. A change here needs a number from the
  player document or from the player team, never a guess.
- **The database holds everything.** Jobs, findings, incidents, samples, snapshots, settings
  and the rendered reports (`reports.content`, LONGBLOB on MySQL). The analyzer host keeps no
  state, so a second instance serves a report it did not render. `RBA_REPORT_STORAGE` is
  `database` by default; `both` mirrors a copy to `RBA_REPORTS_DIR`. Bulk archives and
  evidence bundles are built in memory and streamed. A schema change needs an Alembic
  revision in `backend/alembic/versions/`.
- Every rule needs a positive and a negative test driven by the fault-injecting fixture
  server in `tests/fixtures/`.
- **The analyzer never fails silently.** A handler that raises is logged with its traceback
  and counted on the poller, never swallowed. A measurement that produced nothing carries
  the reason it produced nothing: `AnalysisSession.sampling_state()` explains a session that
  sampled no segment, the reason rides the progress metric, and the UI renders it in place
  of an empty chart. `python -m app.cli diagnose <url>` prints the same account per
  rendition.
- Frontend: React 18 + Vite + TypeScript, Tailwind, ECharts via `echarts-for-react`,
  Zustand + TanStack Query. `MSN_GAP_TOLERANCE = 5` is shared from `src/lib/constants.ts`.
- Design system — **Signal Clarity**. White is the dominant ground; a collapsible dark rail
  (`#0D1430`) anchors navigation. The palette is Samsung blue `#1428A0`, violet `#7B2CBF`
  and TV Plus pink `#FF2D55`, plus one green `#12864C` for a clean result; the gradient
  `#1428A0 → #7B2CBF → #FF2D55` is reserved for the executive verdict and the report header.
  No colour outside that range enters the product, charts and report templates included.
  Severity runs green → blue → violet → pink → filled pink, and colour is never the only
  signal: every severity also carries its word.
- Typography: One UI Sans → SamsungOne → Inter → Arial for the interface; JetBrains Mono →
  Roboto Mono for machine evidence only — manifests, URLs, timestamps, HTTP information,
  identifiers and measured numbers. Never monospace for navigation, headings or prose.
- Tokens live in `frontend/tailwind.config.js`; component classes in `src/theme/index.css`;
  primitives in `src/components/ui/`. The rail and page header are in `src/components/layout/`.
  The same tokens are mirrored in `backend/app/reports/templates/` so a report looks like the
  screen it came from.
- **A tab polls only what is in flight.** A list of finished work — analysed channels, reports,
  a completed job — changes when a run finishes, and the tab that finishes it invalidates the
  query. `isTerminal()` and `LIVE_POLL_MS` in `src/lib/constants.ts` are the one rule: a
  `refetchInterval` returns `false` once every job it watches has stopped. Only `/api/health`
  polls unconditionally, for the rail badge.
- **A job outlives the tab that started it.** Bulk batches run on the backend and are written
  to `jobs` and `bulk_items`, so `GET /api/bulk/jobs` reads the database as well as the
  in-process registry and the tab reattaches to a batch still running after a reload. A job
  listing that only reads memory is a bug.
- Results panels are hidden with CSS, never unmounted, so video element refs survive. There
  is no router, so a channel sent from All channels into Realtime or Aging travels as state:
  `src/lib/prefill.ts` seeds the target tab's form once per send and every field stays
  editable. Nothing auto-starts.
- **CASCADA is the field metric and is measured in percent.** `app/cascada/` reads a
  per-minute `rebuffering_ratio` per channel. A response carries the requested window tagged
  `origin` and the week before it tagged `comparison`; every stated figure comes from `origin`
  alone, and `comparison` exists only for the previous-week overlay. The unit is `%`, so its
  threshold is `cascada_rebuffering_threshold_pct` and never `rebuffer_ratio_threshold`, which
  is a fraction. A channel qualifies as a rebuffering channel on its average, never on one
  minute, and a null minute is a gap rather than a zero. The session is an operator's, pasted
  in Settings and held server-side: a browser cannot hand its CASCADA cookies to another
  origin, and the value is never returned to a page or written to a log.
- **A batch calls the engines, it does not reimplement them.** `app/batch/` composes what
  already exists: `app.cascada.service` for the country listing, the scan and the averaging,
  `app.analysis` through `job_manager.submit()` for every analysis and aging run. A second
  scan loop or a second selection rule in `app/batch/` is a bug. A batch freezes its settings
  in `batches.settings_snapshot` at the moment it starts — including the CASCADA threshold and
  window, which live with the analysis thresholds and are copied in rather than duplicated —
  so a Settings edit never disturbs a running batch and the report states what it used. The
  weekly firing is a `batch_schedules` row read by a loop, never an in-process timer: a
  restart on a Sunday evening must not miss Monday. A skipped firing records its reason, since
  a schedule that skips silently is indistinguishable from one that is broken. Aging follows a
  scheduled batch only; a manual batch starts none.
- The channel catalogue is fetched by the backend, never the browser: it sends no CORS
  headers, and the country/environment to `dbconnect` mapping belongs in one place,
  `app/tvplus/catalogue.py`. The tab reads the country list from `/api/catalogue/countries`
  rather than holding a second copy.
- Stopping a realtime session ends it: the player element is stopped and emptied, the store
  is reset, and the run is filed under Analysed channels. The Realtime tab returns to the
  state it was in before the analysis so the next one starts clean.
- The brand mark, the favicon and the report masthead all come from
  `frontend/public/tvplus-logo.png` and `backend/app/reports/assets/`, generated from the
  Samsung TV Plus icon. Assets are served locally, never hotlinked: the deployment host has
  no route to the internet.

## Deployment

`107.109.131.68` — backend (uvicorn) on **8010**, frontend (nginx) on **8080**.
Port **8001 is reserved by the existing Metanalyser backend and is never used.**
Check with `ss -ltnp` before binding; if a port is taken, take the next free one and record
it in `README.md` and `backend/.env`.
