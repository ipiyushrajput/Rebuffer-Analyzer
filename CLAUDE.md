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
  api/        REST routers (realtime, channels, aging, bulk, reports, proxy, settings, health)
  ws/         WebSocket hub + typed message schemas
  jobs/       job manager, persistence, resume-on-restart
  net/        fetcher (manual redirects + timing split), dns, tls_inspect
  hls/        m3u8 master/media models, URI resolution, SCTE-35
  media/      ts, fmp4, h264_sps, hevc_sps, adts, ffprobe
  analysis/   collectors/, rules/, vpb.py, correlate.py, attribution.py, verdict.py
  reports/    Jinja2 templates + HTML/PDF renderers + escalation blocks
  bulk/       csv/xlsx/json parsing with column alias mapping
  db/         SQLAlchemy models, repository, session factory
frontend/src/
  tabs/       Realtime, Channels (analysed channels), Aging, Bulk, Reports, Settings
  components/ Player, charts/, FindingCard, ManifestViewer, SequenceLadder
```

## Conventions

- Python 3.11+, `ruff` + `mypy --strict` on `app/`, `pytest` with `pytest-asyncio`.
- Every rule is declared in `app/analysis/rules/` and registered in the registry at import
  time. A rule carries `id`, `layer`, `severity`, `owner`, `title`, `root_cause`, `fix`,
  `rebuffer_impact`. Text is written in present-tense definite statements.
- Thresholds are never inlined. They live in `app/config.py` (`Thresholds`), are editable
  in the Settings tab, and are persisted in the DB.
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
- Results panels are hidden with CSS, never unmounted, so video element refs survive.
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
