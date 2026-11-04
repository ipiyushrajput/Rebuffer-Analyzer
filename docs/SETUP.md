# Setup

StreamLens (internally `rba`) is a Python backend and a React frontend. Running it means three
things: a virtualenv with the backend installed, `node_modules` for the frontend, and a
`backend/.env` naming the database. Everything below does those three things; they differ only
in the shell.

| | Ubuntu (and macOS) | Windows |
|---|---|---|
| System packages | `sudo deploy/prereqs-ubuntu.sh` | `winget install …` (below) |
| Install for development | `make install` | `deploy\windows\setup.cmd -Dev` |
| Run both, with reload | `make dev` | `deploy\windows\rba.cmd dev` |
| Backend only | `make backend` | `deploy\windows\rba.cmd backend` |
| Frontend dev server only | `make frontend` | `deploy\windows\rba.cmd frontend` |
| Production bundle, no nginx | `make serve` | `deploy\windows\rba.cmd serve` |
| Production server (systemd + nginx) | `sudo deploy/install.sh` | — (`serve` as a scheduled task) |
| Build the bundle | `make build` | `deploy\windows\rba.cmd build` |
| Database migrations | `make migrate` | `deploy\windows\rba.cmd migrate` |
| Tests | `make test` | `deploy\windows\rba.cmd test` |
| Linters | `make lint` | `deploy\windows\rba.cmd lint` |
| Rule catalogue | `make rules` | `deploy\windows\rba.cmd rules` |
| Headless analysis | `make analyse URL=… ARGS="…"` | `deploy\windows\rba.cmd analyse … ` |
| Why no segments were sampled | `make diagnose URL=…` | `deploy\windows\rba.cmd diagnose …` |
| Clean build artefacts | `make clean` | `deploy\windows\rba.cmd clean` |
| List every command | `make help` | `deploy\windows\rba.cmd help` |

Ports are the same on both: backend **8010**, Vite dev server **5173**, production bundle
**8080**. **8001 is never used** — it belongs to the existing Metanalyser backend, and both
installers refuse it outright.

Ubuntu has two paths. [**Development**](#ubuntu--development) runs from the checkout with
`make`. [**Production server**](#ubuntu--production-server) installs into `/opt/rba` with a
systemd service and an nginx site, and is what `107.109.131.68` runs.

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

## Ubuntu — development

Ubuntu 22.04 and 24.04. macOS works the same way from step 2, with Python, Node and ffmpeg
from Homebrew.

### 1. System packages

```bash
git clone https://github.com/ipiyushrajput/Rebuffer-Analyzer.git
cd Rebuffer-Analyzer

sudo deploy/prereqs-ubuntu.sh
```

That installs Python 3.11+ with `venv`, Node 22, nginx, ffmpeg, rsync, curl and the MySQL
client, and keeps anything already present at a usable version. Ubuntu's own packages are not
enough on their own: 22.04's `python3` is 3.10, so the script adds 3.12 from the deadsnakes
PPA, and both releases' `nodejs` is older than 20, so Node 22 comes from NodeSource. The same
by hand, on 24.04:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-dev build-essential git rsync curl ffmpeg
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

On 22.04, add `sudo add-apt-repository -y ppa:deadsnakes/ppa` and install `python3.12
python3.12-venv python3.12-dev`, then run step 2's install as `make install PYTHON=python3.12`.

Confirm:

```bash
python3 --version    # 3.11 or newer (or python3.12 --version on 22.04)
node --version       # v20 or newer
```

### 2. Install, configure, run

```bash
make install                            # backend/.venv + backend + node_modules
backend/.venv/bin/python -m playwright install --with-deps chromium   # PDF export (optional)
cp backend/.env.example backend/.env    # then fill it in, see "Connecting your database"
make migrate                            # create or upgrade the schema
make dev                                # backend :8010, frontend :5173
```

For a purely local run, `DB_ENGINE=sqlite` in `backend/.env` is enough. Open
<http://localhost:5173>. `make dev` runs both with reload and stops both on Ctrl+C.

### Every Ubuntu command

```bash
make dev          # backend + Vite dev server, both with reload
make backend      # backend only, on PORT (8010)
make frontend     # Vite dev server only, on 5173
make serve        # build the bundle, serve it on FRONTEND_PORT (8080) with the backend
make build        # build the bundle only
make migrate      # alembic upgrade head
make test         # pytest + vitest
make lint         # ruff + mypy + eslint + tsc
make rules        # regenerate docs/RULES.md
make clean        # remove build and cache artefacts
make help         # this list
```

Headless, no UI:

```bash
make analyse URL="https://cdn.example/live/ch1/master.m3u8" ARGS="--duration 5m --html out.html --pdf out.pdf"
make diagnose URL="https://cdn.example/live/ch1/master.m3u8"

# or straight from the venv
cd backend && .venv/bin/python -m app.cli analyse "https://cdn.example/live/ch1/master.m3u8" --duration 5m
```

`analyse` exits **0** when no stream-side defect was found and **2** when one was;
`diagnose` exits **0** when segments were sampled and **1** when none were. Paths in `ARGS`
are relative to `backend/`.

Other ports: `make backend PORT=8011`, `make serve PORT=8011 FRONTEND_PORT=8081`. Port 8001
is refused.

---

## Ubuntu — production server

What `107.109.131.68` runs, and what every instance behind a load balancer runs: the backend
installed in `/opt/rba` as a systemd service (`rba-backend`, user `rba`) on **8010**, and the
built frontend served by nginx on **8080**, which also proxies `/api` and `/ws` to the backend.
Data the backend writes lives in `/var/lib/rba`. For the AWS side — load balancer, RDS, S3,
CloudFront — see [`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md).

### 1. Check the ports

```bash
sudo ss -ltnp | grep -E ':(8001|8010|8080)\b'
```

8001 is Metanalyser's and stays as it is. If 8010 or 8080 is taken, pick the next free port,
pass it to the installer as `RBA_PORT=` / `RBA_FRONTEND_PORT=`, and record it in
`README.md` and `backend/.env`. The installer checks again and stops rather than binding over
anything.

### 2. System packages and the checkout

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/ipiyushrajput/Rebuffer-Analyzer.git /srv/streamlens
cd /srv/streamlens
sudo deploy/prereqs-ubuntu.sh                 # add --with-bento4 for cbcs DRM channels
```

### 3. Install

```bash
sudo deploy/install.sh
```

The installer:

- creates the `rba` system user;
- copies the backend to `/opt/rba/backend` and builds its virtualenv with the newest Python
  3.11+ on the host;
- installs Chromium for PDF export into `/opt/rba/browsers`, with its system libraries;
- writes `/opt/rba/backend/.env` from `.env.example` if there is none (mode 600, data paths
  under `/var/lib/rba`);
- runs the migrations and builds the frontend;
- installs `/etc/systemd/system/rba-backend.service` and `/etc/nginx/conf.d/rba.conf`;
- starts both and prints the health status.

The frontend is built to call `/api` and `/ws` on whatever address served the page, so the
same build works on the IP, on a hostname and behind an HTTPS load balancer. Set
`VITE_API_BASE` / `VITE_WS_BASE` in the environment only to point it somewhere else.

`PREFIX=` (default `/opt/rba`), `DATA_DIR=` (default `/var/lib/rba`) and `PYTHON=` (e.g.
`PYTHON=python3.12`) override the defaults: `sudo PYTHON=python3.12 deploy/install.sh`.

### 4. Fill in `.env`, then run the installer again

On a first install the database is not configured yet, so the health line reads
`degraded - database`. Fill it in and re-run:

```bash
sudo nano /opt/rba/backend/.env     # DB_*, and for production RBA_STORAGE_BACKEND=s3 etc.
sudo deploy/install.sh              # applies the migrations and restarts with the settings
```

[Connecting your database](#connecting-your-database) lists the `DB_*` values;
[`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md) §5 lists the S3, CloudFront, instance
and secrets values. Generate `RBA_SECRETS_KEY` once and use the same value on every instance:

```bash
sudo -u rba /opt/rba/backend/.venv/bin/python -m app.cli secret-key
```

(Run it from `/opt/rba/backend`.)

### 5. Check it

```bash
curl -s http://127.0.0.1:8010/api/health | python3 -m json.tool   # "status": "ok"
curl -s http://127.0.0.1:8080/healthz                             # ok
```

Then open `http://<host>:8080`.

### Running it

```bash
sudo systemctl status rba-backend          # state, PID, memory
sudo systemctl restart rba-backend         # after editing /opt/rba/backend/.env
sudo systemctl stop rba-backend            # running jobs resume on the next start
journalctl -u rba-backend -f               # backend log
journalctl -u rba-backend --since "1 hour ago"
sudo tail -f /var/log/nginx/rba-access.log /var/log/nginx/rba-error.log
sudo nginx -t && sudo systemctl reload nginx
```

### Upgrading

```bash
cd /srv/streamlens
git pull
sudo deploy/install.sh
```

A re-run is an upgrade. It knows its own ports are already bound and does not stop on them.
It keeps `/opt/rba/backend/.env`, the virtualenv, `/var/lib/rba` and the database. It
replaces the code and the bundle, applies the migrations, and restarts the backend. With
several instances, upgrade one first (that one runs the migration), then the others one at
a time: their running jobs move to the other instances while each restarts.

### Backups

`deploy/backup.sh` dumps the MySQL database (RDS has its own snapshots too). Nightly, from
root's crontab (`sudo crontab -e`):

```
30 2 * * * BACKUP_DIR=/var/backups/streamlens /srv/streamlens/deploy/backup.sh >> /var/log/streamlens-backup.log 2>&1
```

It reads the connection from `/opt/rba/backend/.env`. See the comments at the top of the
script for retention and the optional copy to S3.

### Firewall

If `ufw` is enabled, open the frontend port to the networks that use it and keep the backend
port local, since nginx reaches it over `127.0.0.1`:

```bash
sudo ufw allow from 10.0.0.0/8 to any port 8080 proto tcp     # your office / VPN / LB range
sudo ufw deny 8010/tcp
```

### Removing it

```bash
sudo systemctl disable --now rba-backend
sudo rm /etc/systemd/system/rba-backend.service /etc/nginx/conf.d/rba.conf
sudo systemctl daemon-reload && sudo systemctl reload nginx
sudo rm -rf /opt/rba            # code, venv, browsers, .env
sudo rm -rf /var/lib/rba        # local data — skip to keep it
sudo userdel rba
```

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

Add `-Fresh` to delete `backend\.venv` and build it again. Setup already does this on its own
when the existing venv's pip no longer runs; `backend\.env`, the database and the Playwright
browser are not touched either way.

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

One instance per machine. Several instances on different machines can share one MySQL
database: each job runs on exactly one of them (job leases), and a job whose instance stops
is taken over by another. SQLite is for a single instance only.

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
sudo deploy/prereqs-ubuntu.sh --with-bento4   # Ubuntu: Ubuntu does not package Bento4
brew install bento4                           # macOS
```

By hand on Ubuntu, take the Linux SDK zip from <https://www.bento4.com/downloads/> and copy
its `bin/mp4decrypt` to `/usr/local/bin/`.

On Windows, download the Bento4 binaries from <https://www.bento4.com/downloads/> and put
the folder holding `mp4decrypt.exe` on `PATH`.

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

The same on both platforms, and the shortest path if Docker is already installed:

```bash
docker compose up -d --build     # backend :8010, frontend :8080   (Ubuntu: make up)
docker compose exec backend alembic upgrade head
docker compose logs -f           #                                 (Ubuntu: make logs)
docker compose down              #                                 (Ubuntu: make down)
```

Docker Engine with the Compose plugin on Ubuntu, from Docker's own repository:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # then log out and back in
docker compose version
```

On Windows, install Docker Desktop and run the same `docker compose` commands in PowerShell.

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

### Ubuntu

**`Python 3.11 or newer is not installed`.** Ubuntu 22.04's `python3` is 3.10. Run
`sudo deploy/prereqs-ubuntu.sh`, which adds 3.12 from the deadsnakes PPA; the installer
picks `python3.12` up on its own. For `make`, use `make install PYTHON=python3.12`.

**`The virtual environment was not created successfully because ensurepip is not
available`.** The `venv` package is separate on Ubuntu: `sudo apt-get install -y
python3-venv` (or `python3.12-venv`).

**`Node v18… is too old`.** Ubuntu's `nodejs` package is older than 20. Install Node 22 from
NodeSource with `sudo deploy/prereqs-ubuntu.sh`, or by hand as in step 1 above.

**`Port 8010 is already bound`.** `sudo ss -ltnp | grep :8010` names the process. A re-run
of `deploy/install.sh` on a host where it is already installed does not stop on its own
ports.

**The backend does not start.** `journalctl -u rba-backend -n 100 --no-pager` shows why;
the usual causes are a typo in `/opt/rba/backend/.env` or a database the host cannot reach.
`sudo systemctl restart rba-backend` after fixing it.

**`502 Bad Gateway` on :8080.** nginx is up and the backend is not:
`systemctl status rba-backend`. If the backend runs on a port other than 8010, re-run the
installer with that `RBA_PORT` so the nginx site points at it.

**`nginx rejected its configuration`.** `sudo nginx -t` names the file and line. On a host
without IPv6, Ubuntu's default site (`listen [::]:80`) fails the test; remove
`/etc/nginx/sites-enabled/default` if nothing else uses it.

**PDF export fails, HTML works.** Chromium is missing or cannot load its libraries:
`sudo PLAYWRIGHT_BROWSERS_PATH=/opt/rba/browsers /opt/rba/backend/.venv/bin/python -m
playwright install --with-deps chromium`, then `sudo chown -R rba:rba /opt/rba/browsers`.

**`Permission denied` under `/var/lib/rba` or `/opt/rba`.** Files created by hand as root:
`sudo chown -R rba:rba /opt/rba /var/lib/rba`.

### Windows

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

**`ModuleNotFoundError: No module named 'pip._vendor.…'` during setup.** The pip inside
`backend\.venv` is damaged — a pip upgrade was interrupted, or a file was locked or quarantined
while it was written. Setup checks pip before using it and rebuilds the venv when it does not
run; `setup.cmd -Fresh` forces the rebuild. If the delete is refused, setup names the processes
running from the venv: stop them (a running `rba.cmd dev`, a terminal with the venv activated)
and run it again. If it recurs on a fresh venv, check Windows Security → Protection history
for quarantined files, and keep the checkout out of a OneDrive-synced folder.

**`Port 8010 is already bound by …`.** The script names the process. Stop it, or set
`$env:RBA_PORT` to the next free port and record it.

**PDF export fails, HTML works (Windows).** The Playwright browser is missing. Run
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
