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

Backend CLI for headless use:

```
python -m app.cli analyse <url> --duration 5m --html out.html
python -m app.cli rules --markdown            # rule catalogue
```

## Layout

```
backend/app/
  api/        REST routers (realtime, aging, bulk, reports, proxy, settings, health)
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
  tabs/       Realtime, Aging, Bulk, Reports, Settings
  components/ Player, charts/, FindingCard, ManifestViewer, SequenceLadder
```

## Conventions

- Python 3.11+, `ruff` + `mypy --strict` on `app/`, `pytest` with `pytest-asyncio`.
- Every rule is declared in `app/analysis/rules/` and registered in the registry at import
  time. A rule carries `id`, `layer`, `severity`, `owner`, `title`, `root_cause`, `fix`,
  `rebuffer_impact`. Text is written in present-tense definite statements.
- Thresholds are never inlined. They live in `app/config.py` (`Thresholds`), are editable
  in the Settings tab, and are persisted in the DB.
- Every rule needs a positive and a negative test driven by the fault-injecting fixture
  server in `tests/fixtures/`.
- Frontend: React 18 + Vite + TypeScript, Tailwind, light theme, Space Grotesk (UI) and
  IBM Plex Mono (manifests/evidence), ECharts via `echarts-for-react`, Zustand + TanStack
  Query. `MSN_GAP_TOLERANCE = 5` is shared from `src/lib/constants.ts`.
- Results panels are hidden with CSS, never unmounted, so video element refs survive.

## Deployment

`107.109.131.68` — backend (uvicorn) on **8010**, frontend (nginx) on **8080**.
Port **8001 is reserved by the existing Metanalyser backend and is never used.**
Check with `ss -ltnp` before binding; if a port is taken, take the next free one and record
it in `README.md` and `backend/.env`.
