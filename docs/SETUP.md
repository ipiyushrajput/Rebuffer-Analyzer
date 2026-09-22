# Setup

RBA is a Python backend and a React frontend. Running it means three things: a virtualenv
with the backend installed, `node_modules` for the frontend, and a `backend/.env` naming the
database. Everything below does those three things; they differ only in the shell.

| | Linux / macOS | Windows |
|---|---|---|
| Install | `make install` | `deploy\windows\setup.cmd -Dev` |
| Run both | `make dev` | `deploy\windows\rba.cmd dev` |
| Backend only | `make backend` | `deploy\windows\rba.cmd backend` |
| Production bundle | `deploy/install.sh` (nginx) | `deploy\windows\rba.cmd serve` |
| Tests | `make test` | `deploy\windows\rba.cmd test` |
| Linters | `make lint` | `deploy\windows\rba.cmd lint` |
| Headless analysis | `python -m app.cli analyse …` | `deploy\windows\rba.cmd analyse …` |
| Why no segments were sampled | `python -m app.cli diagnose …` | `deploy\windows\rba.cmd diagnose …` |

Ports are the same on both: backend **8010**, Vite dev server **5173**, production bundle
**8080**. **8001 is never used** — it belongs to the existing Metanalyser backend, and both
installers refuse it outright.

---

## Prerequisites

| | Version | Required |
|---|---|---|
| Python | 3.11 or newer | yes |
| Node.js | 20 LTS or newer (ships npm) | yes |
| ffmpeg / ffprobe | any recent build | no — only the decode-error and quality detectors need it |
| MySQL / MariaDB / PostgreSQL | any recent version | no — SQLite runs the whole app locally |

Without ffmpeg the app starts and reports itself **degraded** in the rail; every check except
the decode-error and quality detectors still runs. Without a database, set `DB_ENGINE=sqlite`
and the backend keeps its state in `backend/var/rba.db`.

---

## Linux and macOS

```bash
git clone https://github.com/ipiyushrajput/Rebuffer-Analyzer.git
cd Rebuffer-Analyzer

make install                            # virtualenv + backend + node_modules
cp backend/.env.example backend/.env    # then fill it in, see "Connecting your database"
make dev                                # backend :8010, frontend :5173
```

Open <http://localhost:5173>.

`make dev` runs both with reload and stops both on Ctrl+C. `make help` lists every target.

For the deployment host, `sudo deploy/install.sh` does the whole thing — virtualenv,
migrations, frontend build, a systemd unit and an nginx site. See
[the README](../README.md#deployment-on-10710913168).

---

## Windows

Nothing has to be installed globally beyond Python and Node, and no execution policy has to
be changed on the machine — the `.cmd` wrappers pass `-ExecutionPolicy Bypass` for their own
invocation only.

### 1. Install Python and Node

```powershell
winget install Python.Python.3.12      # tick "Add python.exe to PATH" if prompted
winget install OpenJS.NodeJS.LTS
winget install Gyan.FFmpeg             # optional, enables the decode-error detectors
```

Close and reopen the terminal afterwards so `PATH` is picked up, then confirm:

```powershell
python --version    # Python 3.11 or newer
node --version      # v20 or newer
```

If `python` opens the Microsoft Store instead of printing a version, turn off the Store alias
under **Settings → Apps → Advanced app settings → App execution aliases**, or install Python
from <https://python.org>. The setup script also accepts the `py -3` launcher.

### 2. Clone and set up

```powershell
git clone https://github.com/ipiyushrajput/Rebuffer-Analyzer.git
cd Rebuffer-Analyzer

.\deploy\windows\setup.cmd -Dev
```

That creates `backend\.venv`, installs the backend (with pytest, ruff and mypy because of
`-Dev`), downloads the Playwright Chromium used for PDF export, copies `backend\.env.example`
to `backend\.env`, and installs the frontend packages. It is safe to re-run — it brings an
existing checkout up to date rather than starting over.

Add `-SkipBrowser` to skip the ~150 MB Chromium download. PDF export is then unavailable and
HTML export still works.

### 3. Fill in `backend\.env`

See [Connecting your database](#connecting-your-database). For a purely local run, one
line is enough:

```
DB_ENGINE=sqlite
```

### 4. Run it

```powershell
.\deploy\windows\rba.cmd dev
```

Backend on <http://localhost:8010/api/health>, frontend on <http://localhost:5173>. Ctrl+C
stops both.

To run the production bundle instead of the dev server — the closest thing to how it runs
behind nginx, and what to use when showing it to someone:

```powershell
.\deploy\windows\rba.cmd serve      # builds, then serves on :8080 next to the backend
```

### Every Windows command

```powershell
.\deploy\windows\rba.cmd dev        # backend + Vite dev server, both with reload
.\deploy\windows\rba.cmd backend    # backend only, on RBA_PORT (8010)
.\deploy\windows\rba.cmd frontend   # Vite dev server only, on 5173
.\deploy\windows\rba.cmd serve      # build the bundle, serve it on 8080 with the backend
.\deploy\windows\rba.cmd build      # build the bundle only
.\deploy\windows\rba.cmd test       # pytest + vitest
.\deploy\windows\rba.cmd lint       # ruff + mypy + eslint + tsc
.\deploy\windows\rba.cmd rules      # regenerate docs\RULES.md
.\deploy\windows\rba.cmd clean      # remove build and cache artefacts
.\deploy\windows\rba.cmd help       # this list, with detail
```

Headless, no UI — the exit code is the verdict, so this drops straight into a scheduled task:

```powershell
.\deploy\windows\rba.cmd analyse "https://cdn.example/live/ch1/master.m3u8" `
    --duration 5m --html out.html --pdf out.pdf
```

`analyse` exits **0** when no stream-side defect was found and **2** when one was.

When a run reports **0 segments checked**, `diagnose` says why. It prints the resolved
ladder and, per rendition, the HTTP status, how many segments the playlist listed, how many
this session sampled, and any failure inside the analyzer with its traceback:

```powershell
.\deploy\windows\rba.cmd diagnose "https://cdn.example/live/ch1/master.m3u8"
```

`diagnose` exits **0** when segments were sampled and **1** when none were.

### Changing the ports

Both scripts read `RBA_PORT` and `RBA_FRONTEND_PORT`, check the port before anything binds,
and name the process holding it if it is taken:

```powershell
$env:RBA_PORT = 8011
$env:RBA_FRONTEND_PORT = 8081
.\deploy\windows\rba.cmd serve
```

Record a changed port in `README.md` and `backend\.env`. Port 8001 is refused outright.

### Running it unattended

For a machine that should bring RBA up on its own, register `serve` as a scheduled task that
runs at startup:

```powershell
$action  = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument '/c "C:\path\to\Rebuffer-Analyzer\deploy\windows\rba.cmd" serve' `
    -WorkingDirectory 'C:\path\to\Rebuffer-Analyzer'
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName 'RBA' -Action $action -Trigger $trigger -RunLevel Highest
```

Run **one instance only**. Jobs are held in the backend process and resumed from the database
on restart, so a second instance would run every aging job twice.

---

## Connecting your database

> **"RBA" is this application**, not a database — TV Plus **R**e**b**uffer **A**nalyzer. The
> `rba` you see in `DB_NAME` is the name of the database *it creates inside your SQL server*,
> and the `RBA_*` variables are this app's own settings. There is no separate store to
> replace: point `DB_*` at your server and everything goes there.

### 1. Write the credentials into `backend/.env`

`backend/.env` is git-ignored and must stay that way — it carries the password.
`backend/.env.example` lists every variable.

```
DB_ENGINE=mysql          # mysql | mariadb | postgres | sqlite
DB_HOST=10.0.0.5
DB_PORT=3306
DB_USER=rba
DB_PASSWORD=…
DB_NAME=rba
```

### 2. Give the account the right grants

RBA creates its own database and tables on first start, and never touches another
application's tables:

```sql
CREATE USER 'rba'@'%' IDENTIFIED BY '…';
CREATE DATABASE rba CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON rba.* TO 'rba'@'%';
FLUSH PRIVILEGES;
```

If the account **cannot create databases**, point `DB_NAME` at a database it already has and
set a prefix instead. Every RBA table is then created inside that database under the prefix,
and nothing else in it is touched:

```
DB_NAME=existing_db
DB_TABLE_PREFIX=rba_
```

### 3. Create the schema

The backend creates its tables on first start. On a database you intend to keep, run the
migrations instead so later upgrades apply cleanly:

```bash
cd backend && .venv/bin/alembic upgrade head      # Windows: .venv\Scripts\alembic upgrade head
```

### 4. Check the connection

```
http://localhost:8010/api/health
```

`checks.database.ok` is `true` when the connection works. When it is false the `degraded`
list names `database`, the rail in the UI says so, and the reason class is in the backend
log — the host, user and password are never logged.

### What ends up in the database

Everything: jobs, findings, incidents, playlist and segment samples, player samples, the
virtual-buffer series, playlist snapshots, thresholds, and **the rendered reports
themselves**. The analyzer host keeps no state, so a second instance serves a report it did
not render and replacing a container loses nothing.

`RBA_REPORT_STORAGE` decides where a report goes:

| Value | Behaviour |
|---|---|
| `database` | The bytes go in `reports.content` and nothing is written to the host. **The default.** |
| `both` | The database, plus a mirrored copy in `RBA_REPORTS_DIR`. |
| `disk` | Files only, as builds before this behaved. |

An HTML report is about 1 MB, a PDF about 250 KB, and a bulk archive roughly 10 MB per
50 channels. If you run large batches, raise MySQL's `max_allowed_packet` above the largest
archive you expect:

```sql
SET GLOBAL max_allowed_packet = 268435456;   -- 256 MB; also set it in my.cnf to persist
```

Evidence archives are built in memory from the stored rows and streamed, so they never touch
the host at all.

### Running without a database

```
DB_ENGINE=sqlite
```

Everything above still applies — reports included — except the file lives at
`backend/var/rba.db`. Delete it to start clean. Good for a laptop, not for the deployment:
SQLite takes one writer at a time.

The remaining variables — `RBA_PORT`, the concurrency limits, retention — have working
defaults and only need setting when you want to change them.

---

## Protected channels

Nothing here is needed to analyse clear channels, and nothing here is asked of an analyst per
channel. It is configured once, by whoever sets the deployment up, and after that a Widevine
playback URL is pasted into Realtime, Aging, Bulk or an automated batch exactly like a clear
one.

Set it either in the **Settings → DRM** panel, which stores it in the settings table, or in
`backend/.env` to pin it to this host:

```
RBA_DRM_ENABLED=true
DRM_LICENSE_URL=            # the Widevine licence server the player acquires a licence from
CPIX_ENDPOINT=              # empty uses the KeyOS v4 endpoint
CPIX_CLIENT_CERT=           # a path on this host, or an HTTPS URL
CPIX_CLIENT_KEY=            # a path on this host, or an HTTPS URL
CPIX_SERVER_CERT=           # the key server's own certificate
CPIX_CONTENT_ID=rba
```

**The three CPIX values are a location, never key material.** No certificate, private key or
content key belongs in `backend/.env.example`, in git, or in a log. Keep the private key
behind authentication: whoever can read it can read every content key this deployment
obtains. The settings endpoint reports each one as set or not set, never its value.

The host needs a route to the CPIX endpoint and to the licence server. Without one, a
protected channel is still polled and measured for transport, timing and playlist defects,
and its report states per rendition that the payload was not read and why.

### Playing a protected channel in the preview

**The analysis needs none of this.** The backend obtains the key over CPIX and decrypts the
segments itself, so every finding, chart and report on a protected channel is produced
whether the preview plays or not. What follows is only about the picture in the Realtime tab.

Encrypted Media Extensions are available only in a **secure context**, which is a property of
the page's origin and of nothing else. `navigator.requestMediaKeySystemAccess` is undefined
on a plain `http://` origin that is not localhost, so the preview stops with "The browser
refused Widevine key-system access at http://…" and names the remedy. Neither the stream nor
the licence server has to be HTTPS; only the page.

Three ways to get one, in the order they cost anything:

| Route | Certificate | What it takes |
|---|---|---|
| Open the app at `http://localhost:8080` **on the deployment host itself** | none | Nothing. `localhost` and `127.0.0.1` are secure contexts by definition. |
| Trust the origin in Chrome: `--unsafely-treat-insecure-origin-as-secure=http://107.109.131.68:8080`, or the `OverrideSecurityRestrictionsOnInsecureOrigin` enterprise policy | none | A flag per machine, or one policy for the fleet. Designed for internal deployments. |
| Serve the page over HTTPS | self-signed or an internal CA | An nginx `listen 443` block and a certificate the browsers trust. |

The preview logs the licence lifecycle into the run's own event log — the key session opening,
the licence loading, the first fragment decrypting — so a DRM failure is evidence in the
analysis rather than a console line nobody kept.

**Settings → DRM → "Licence request path"** decides where the browser posts the challenge.
*Relay* (the default) posts to this application's `/api/drm/license`, which forwards it from
the analyzer host: that works whatever the licence server's CORS policy is, and keeps the
server's address off the page. *Direct* posts to the licence server from the browser, which
works only where that server allows a cross-origin POST — and establishes whether it does.

### Decrypting `cbcs` channels

`cenc` (AES-CTR) is decrypted in this process, with no binary installed: that is every TV
Plus channel measured so far, and nothing below is needed for one.

`cbcs` is a different cipher — AES-CBC with a crypt/skip pattern — and is decrypted only by
Bento4's `mp4decrypt`. Install it and it is used automatically for those tracks:

```bash
sudo apt-get install -y bento4            # Debian and Ubuntu
brew install bento4                       # macOS
```

`GET /api/health` reports whether it was found. Without it a `cbcs` channel's transport,
timing and playlist checks still run, and each protected rendition says through `INFO-001`
and the report's protection table that its payload was not read, and why.

### Evidence bundles from a protected channel

An evidence bundle carries the sampled segments as the CDN served them, under `encrypted/`.
**Settings → DRM → "Include decrypted media in evidence bundles"** adds the decrypted copies
beside them, under `decrypted/video/` and `decrypted/audio/`, each with its initialisation
segment in front so a decoder opens a file rather than a fragment, plus a
`decrypted/manifest.json` mapping every file to its source URI, media sequence number and
rendition.

It is **off by default**: decrypted media is the content in the clear, in a file that gets
forwarded to whoever a defect belongs to. Turn it on where that is acceptable and where a
packager needs to reproduce a bitstream defect. No content key, private key or CPIX
credential is written to a bundle either way, and the bundle's `README.txt` states which of
the reasons applies when it holds no decrypted media.

---

## With Docker

The same on both platforms, and the shortest path if Docker Desktop is already installed:

```bash
docker compose up -d --build     # backend :8010, frontend :8080
docker compose logs -f
docker compose down
```

---

## Checking it works

```
http://localhost:8010/api/health
```

`status` is `ok` when every dependency answers, and `degraded` with a `degraded` list naming
what is missing — the rail shows the same thing at the bottom left. A degraded analyzer still
analyses; it just says which detectors are not running.

Then paste a playback URL into the **Realtime** tab and press **Analyse stream**. The verdict
appears once the first playlist and segment measurements complete.

---

## When something does not start

**`python` opens the Microsoft Store.** Turn off the execution alias under Settings → Apps →
Advanced app settings → App execution aliases, or install Python from python.org.

**`running scripts is disabled on this system`.** Use the `.cmd` wrappers
(`setup.cmd`, `rba.cmd`) rather than calling the `.ps1` files directly; they bypass the policy
for that one invocation without changing the machine.

**`The string is missing the terminator` / `Missing closing '}'`.** Windows PowerShell 5.1
reads a `.ps1` file without a byte-order mark as ANSI rather than UTF-8, so any non-ASCII
character — an em dash, a curly quote, an arrow — turns into three characters, the last of
which can close a string and break the parse hundreds of lines further down. The scripts in
`deploy\windows\` are kept to plain ASCII for exactly this reason, and
`tests/test_deploy_scripts.py` fails the build if a non-ASCII byte gets in. If you edit one,
keep it ASCII; `git diff` after saving is the quickest way to spot an editor that helpfully
replaced `-` with `—`.

**`Port 8010 is already bound by …`.** The script names the process. Stop it, or set
`$env:RBA_PORT` to the next free port and record it.

**PDF export fails, HTML works.** The Playwright browser is missing. Run
`backend\.venv\Scripts\python.exe -m playwright install chromium`, or point
`RBA_CHROMIUM_EXECUTABLE` at a Chrome you already have.

**The health check lists `ffmpeg`.** Expected without ffmpeg installed. Only the decode-error
and quality detectors are affected; install it with `winget install Gyan.FFmpeg` to enable
them.

**`NotImplementedError` from `asyncio.create_subprocess_exec` during an analysis.** Fixed —
the quality detectors no longer use an asyncio subprocess. It happened because `uvicorn`
selects `SelectorEventLoop` on Windows whenever it is started with `--reload` (which `rba.cmd
dev` does), and that loop implements no subprocess transport, so every ffprobe call failed on
a development run while working on the deployed one. ffprobe and ffmpeg now run on a worker
thread, which behaves the same under whichever loop the server chose.

**The health check lists `database`.** The backend cannot reach the database named in
`backend/.env`. Jobs and reports are not persisted until it can. The startup log says why —
`database schema creation failed: <reason>` — with the password removed from the reason. Set
`DB_ENGINE=sqlite` to run against a local file instead.

**`ImportError: cannot import name 'escape_bytes_prefixed' from 'pymysql.converters'`.** The
venv holds PyMySQL 1.1.2 or newer, which removed a name `aiomysql` imports, so every database
call fails. `aiomysql` asks only for `PyMySQL>=1.0`, so a venv built before the upper bound
was added can hold a release it cannot use. Re-run the installer, or fix the one package:

```
backend\.venv\Scripts\python.exe -m pip install "pymysql>=1.1,<1.1.2"
```

**A long path error during `npm install` on Windows.** Enable long paths once:
`git config --system core.longpaths true`, and set `LongPathsEnabled` to `1` under
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`.
