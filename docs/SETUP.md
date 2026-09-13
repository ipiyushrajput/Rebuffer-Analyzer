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
| MySQL | the Transcoder instance | no — SQLite runs the whole app locally |

Without ffmpeg the app starts and reports itself **degraded** in the rail; every check except
the decode-error and quality detectors still runs. Without a database, set `DB_ENGINE=sqlite`
and the backend keeps its state in `backend/var/rba.db`.

---

## Linux and macOS

```bash
git clone https://github.com/ipiyushrajput/Rebuffer-Analyzer.git
cd Rebuffer-Analyzer

make install                            # virtualenv + backend + node_modules
cp backend/.env.example backend/.env    # then fill it in, see "The .env file" below
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

See [The .env file](#the-env-file). For a purely local run, one line is enough:

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

## The .env file

`backend/.env` is git-ignored and must stay that way — it carries the database password.
`backend/.env.example` lists every variable with empty values.

Against the shared MySQL instance:

```
DB_ENGINE=mysql
DB_HOST=<host>
DB_PORT=3306
DB_USER=<user>
DB_PASSWORD=<password>
DB_NAME=rba
```

RBA creates its own `rba` database and never touches the Transcoder tables. If the account
cannot create databases, leave `DB_NAME` pointing at the existing one and set
`DB_TABLE_PREFIX=rba_`; every RBA table is then created inside it under that prefix.

Locally, without any database at all:

```
DB_ENGINE=sqlite
```

State then lives in `backend/var/rba.db`. Delete that file to start clean.

The remaining variables — `RBA_PORT`, `RBA_DATA_DIR`, the concurrency limits — have working
defaults and only need setting when you want to change them.

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

**`Port 8010 is already bound by …`.** The script names the process. Stop it, or set
`$env:RBA_PORT` to the next free port and record it.

**PDF export fails, HTML works.** The Playwright browser is missing. Run
`backend\.venv\Scripts\python.exe -m playwright install chromium`, or point
`RBA_CHROMIUM_EXECUTABLE` at a Chrome you already have.

**The health check lists `ffmpeg`.** Expected without ffmpeg installed. Only the decode-error
and quality detectors are affected; install it with `winget install Gyan.FFmpeg` to enable
them.

**The health check lists `database`.** The backend cannot reach the database named in
`backend/.env`. Jobs and reports are not persisted until it can. Set `DB_ENGINE=sqlite` to
run against a local file instead.

**A long path error during `npm install` on Windows.** Enable long paths once:
`git config --system core.longpaths true`, and set `LongPathsEnabled` to `1` under
`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`.
