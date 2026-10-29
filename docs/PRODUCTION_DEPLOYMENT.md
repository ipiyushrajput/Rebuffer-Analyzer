# StreamLens — production deployment

How to run StreamLens (internally `rba`) in production:
- one or more instances behind a load balancer;
- MySQL for state;
- S3 for files, read through CloudFront.

The infra team's checklist is in [`INFRA_REQUEST.md`](INFRA_REQUEST.md). Local setup for
developers is in [`SETUP.md`](SETUP.md).

**Port 8001 belongs to the Metanalyser backend and is never used.**

---

## 1. Architecture

```
                         Operators' browsers
                                 │ https://streamlens.<domain>
                                 ▼
                ┌────────────────────────────────┐
                │ Load balancer (TLS, sticky)     │  /api/*, /ws/* → instance :8080
                └───────┬────────────────┬───────┘
                        ▼                ▼
        ┌──────────────────────┐  ┌──────────────────────┐
        │ Instance A            │  │ Instance B            │   N identical instances
        │ nginx :8080           │  │ nginx :8080           │   (install.sh or Docker)
        │  └ backend :8010      │  │  └ backend :8010      │
        │ ffmpeg, Chromium      │  │ ffmpeg, Chromium      │
        │ /var/lib/rba (spool)  │  │ /var/lib/rba (spool)  │
        └──────┬───────┬───────┘  └──────┬───────┬───────┘
               │       │ IAM role         │       │
               ▼       ▼                  ▼       ▼
       ┌──────────────┐  ┌──────────────────────────────┐
       │ MySQL (RDS)  │  │ S3 bucket (private, SSE)      │◄── CloudFront (OAC, signed URLs)
       │ jobs, leases │  │ channels/<channel>/<day>/<run> │       ▲
       └──────────────┘  └──────────────────────────────┘       │ downloads (302 from /api)
                                                            Operators' browsers
        Outbound from every instance: stream CDNs, CASCADA, KeyOS CPIX, licence server, DNS
```

| Piece | What it holds |
|---|---|
| **MySQL** | Jobs and job leases, findings, incidents, samples, the playlist poll log, settings, report index rows. |
| **S3** | Report files (`RBA_REPORT_STORAGE=storage`), recordings, and each finished run's `network-log.zip` and `evidence.zip`, one folder per channel. |
| **CloudFront** | Every download. The backend answers a download link with a redirect to a signed CloudFront URL. |
| **Instance disk** | Only a recording *while its run is writing it*. It moves to S3 when the run ends. |

### 1.1 How files are laid out in S3

```
<RBA_S3_PREFIX>/channels/<channel-slug>-<channel-id>/<YYYY-MM-DD>/<job-id>/
    reports/<report>.html|.pdf                tag retention=keep
    archives/network-log.zip                  tag retention=pinned
    archives/evidence.zip                     tag retention=pinned
    recording/full/...  + recording.zip       tag retention=standard   (realtime)
    recording/clips/... + recording.zip       tag retention=pinned     (aging incident clips)
```

- `<channel-id>` is a short hash of the channel's playback URL without its query, so every run
  of a channel shares one folder, even across renamed channels and re-signed tokens.
- Batch summary reports (CSV/XLSX) go under `batches/<batch-id>/`.
- Deleting a run in the UI deletes its folder.
- **Retention is the bucket's lifecycle rules, keyed on the `retention` tag** (§4.3).

### 1.2 How several instances share the work

- **Leases.** Every job (realtime, aging, bulk channel) runs on the instance holding its lease
  in MySQL, renewed every `RBA_JOB_LEASE_S / 3` seconds.
- **Takeover.** If an instance stops, its leases lapse after `RBA_JOB_LEASE_S`. Another
  instance then takes the aging and bulk jobs over and resumes them for their remaining time.
  Exactly one instance wins each job.
- **Stop button.** Stop works from any instance: it writes `stop_requested`, and the owner
  stops the run on its next renewal.
- **Weekly batches.** The weekly batch scheduler runs on every instance, and each firing is
  claimed by exactly one.
- **Sticky sessions are required.** A realtime analysis streams to the browser over a
  WebSocket (`/ws/…`) from the instance running it, and the browser player posts its
  telemetry to that instance. The load balancer must keep a browser on one instance (§3).
- **Limitations.** An automated batch, and a bulk batch's *coordination*, run on the instance
  that started them. If that instance stops mid-batch:
  - the analyses already started are taken over by lease;
  - channels not yet started are not, and the batch shows what completed.

---

## 2. Instance requirements

| Resource | Per instance | Notes |
|---|---|---|
| OS | Ubuntu 22.04/24.04 LTS or Debian 12, x86-64 | Docker images are Debian 12 based. |
| CPU | 8 vCPU (4 minimum) | Up to `RBA_MAX_CONCURRENT_JOBS` (20) analyses; `/api/metrics` reports event-loop lag. |
| RAM | 16 GB (8 minimum) | Backend capped at 6 GB (`MemoryMax`, Compose `memory`). |
| Disk | 100 GB | OS, venv with Chromium (~1 GB), logs, and recordings of runs *in progress* (spool). |
| Network | Outbound internet | Stream CDNs, CASCADA, KeyOS, licence server, S3, PyPI/npm at install. |
| Time | NTP synchronised | Leases and every timing rule depend on the clock. |

### Software

**Bare metal (`deploy/install.sh`):**

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 | |
| Node | 20 or 22 LTS | Build only. |
| nginx | 1.18+ | |
| ffmpeg / ffprobe | — | |
| `rsync`, `iproute2` | — | |
| MySQL client | — | Needed for `deploy/backup.sh`. |
| Chromium for PDF export | — | `python -m playwright install chromium` and `playwright install-deps chromium`. |
| Bento4 `mp4decrypt` | — | Optional, for `cbcs` DRM. |
| AWS CLI | — | Optional, only for `BACKUP_S3_URI`. |

**Docker:** Docker Engine 24+ with the Compose v2 plugin. The images carry Python, ffmpeg,
Chromium and nginx.

### Ports

| Port | Who listens | Exposed to |
|---|---|---|
| 8080 | nginx (UI, `/api`, `/ws`) | The load balancer only |
| 8010 | uvicorn backend | localhost (bare metal); the Compose network (Docker) |
| 8001 | **Metanalyser — never used** | — |

---

## 3. Load balancer

- HTTPS listener with the certificate for the StreamLens hostname. HTTPS is also what makes the
  DRM preview's EME work in Chrome.
- Target: every instance on **8080**.
- Health check: `GET /healthz` on 8080, which returns `ok`. For readiness behind it, use
  `GET /api/health/ready`.
- **Stickiness: on**, cookie-based (application or LB cookie), duration ≥ 24 h.
- **WebSocket support** on `/ws/*`, with an idle timeout of at least 1 hour. An aging run's
  progress stream can stay open for up to 24 hours; the UI reconnects if it drops.
- Request body limit ≥ 64 MB (bulk uploads).
- Response timeout ≥ 300 s for `/api/*`: report and archive generation streams.
- Restrict `/api/metrics` to the monitoring network (an LB rule or a WAF rule).

---

## 4. AWS resources

### 4.1 S3 bucket

- Private: Block Public Access on all four settings.
- Default encryption SSE-S3 (`AES256`). With SSE-KMS, add the KMS permissions in §4.4.
- Versioning: optional, recommended for reports.
- No CORS configuration needed: downloads are browser navigations to CloudFront.

### 4.2 CloudFront

- Origin: the bucket, with **Origin Access Control**. The bucket policy allows only this
  distribution.
- Viewer protocol: HTTPS only.
- **Restrict viewer access with a trusted key group** (signed URLs). Without it, anyone holding
  a link can download it. The backend signs every URL it hands out for `RBA_CDN_URL_TTL_S`
  (default 1 h).
- Cache policy: CachingOptimized. Objects are written once and never change.
- Optional: AWS WAF with an IP allowlist of the office/VPN ranges.

### 4.3 Lifecycle rules (on the bucket)

| Rule | Filter | Action |
|---|---|---|
| Full recordings | tag `retention=standard` | Expire after 30 days (`RBA_SAMPLE_RETENTION_DAYS`) |
| Clips and archives | tag `retention=pinned` | Expire after 180 days (`RBA_PINNED_RETENTION_DAYS`) |
| Reports | tag `retention=keep` | No expiry (optionally Glacier after 90 days) |
| Multipart | whole bucket | Abort incomplete multipart uploads after 7 days |

### 4.4 IAM role for the instances

Attach to the instance profile (EC2), the task role (ECS) or IRSA (EKS). No access keys.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StreamLensObjects",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetObjectTagging",
        "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::<bucket>/<prefix>*"
    },
    {
      "Sid": "StreamLensList",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::<bucket>",
      "Condition": {"StringLike": {"s3:prefix": ["<prefix>*"]}}
    }
  ]
}
```

- **With SSE-KMS:** add `kms:GenerateDataKey` and `kms:Decrypt` on the key.
- **With `BACKUP_S3_URI`:** add `s3:PutObject` on the backup prefix.

### 4.5 MySQL (RDS or equivalent)

- MySQL 8.0, InnoDB, `utf8mb4` / `utf8mb4_unicode_ci`.
- Parameter group:
  - `max_allowed_packet = 268435456` (256 MB);
  - `wait_timeout ≥ 28800`;
  - `time_zone = UTC`.
- Connections: each instance opens up to `RBA_DB_POOL_SIZE + RBA_DB_MAX_OVERFLOW` (40 at the
  defaults). So `max_connections` ≥ 40 × instances + headroom.
- Automated backups with point-in-time recovery, plus Multi-AZ for production.
  `deploy/backup.sh` is the equivalent for a self-managed server.
- Network: reachable from the instances only (security group).

```sql
CREATE DATABASE rba CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'rba'@'%' IDENTIFIED BY '<strong password>';
GRANT ALL PRIVILEGES ON rba.* TO 'rba'@'%';
```

---

## 5. Configuration (`backend/.env` on every instance)

`chmod 600`, never in git. Every instance gets the same file, except `RBA_INSTANCE_ID`.

```ini
# Database
DB_ENGINE=mysql
DB_HOST=<rds endpoint>
DB_PORT=3306
DB_USER=rba
DB_PASSWORD=<secret>
DB_NAME=rba
RBA_DB_POOL_SIZE=20
RBA_DB_MAX_OVERFLOW=20

# Service
RBA_PORT=8010
RBA_PUBLIC_ORIGIN=https://streamlens.<domain>
RBA_CORS_ORIGINS=https://streamlens.<domain>
RBA_MAX_CONCURRENT_JOBS=20

# Storage: S3 behind CloudFront
RBA_STORAGE_BACKEND=s3
RBA_REPORT_STORAGE=storage
RBA_S3_BUCKET=<bucket>
RBA_S3_PREFIX=<prefix, e.g. prod>
RBA_S3_REGION=<region>
RBA_CDN_BASE_URL=https://<distribution>.cloudfront.net
RBA_CDN_KEY_PAIR_ID=<public key id from the key group>
RBA_CDN_PRIVATE_KEY=/etc/streamlens/cloudfront-private-key.pem
RBA_CDN_URL_TTL_S=3600
RBA_ARCHIVE_ARTIFACTS=true

# Local spool for recordings in progress, and the data dir
RBA_DATA_DIR=/var/lib/rba
RBA_RECORDINGS_DIR=/var/lib/rba/recordings
RBA_STORAGE_DIR=/var/lib/rba/storage
RBA_RECORDINGS_QUOTA_GB=50

# Several instances
RBA_INSTANCE_ID=<instance name, e.g. streamlens-a>
RBA_JOB_LEASE_S=60

# Retention (keep in step with the bucket lifecycle rules)
RBA_SAMPLE_RETENTION_DAYS=30
RBA_PINNED_RETENTION_DAYS=180

# Encrypts the CASCADA session and the alert webhook in the database
RBA_SECRETS_KEY=<python -m app.cli secret-key>

# DRM (locations, never key material)
DRM_LICENSE_URL=
CPIX_ENDPOINT=
CPIX_CLIENT_CERT=/etc/streamlens/cpix/client.crt
CPIX_CLIENT_KEY=/etc/streamlens/cpix/client.key
CPIX_SERVER_CERT=/etc/streamlens/cpix/server.crt
```

- **Storage across instances:** with more than one instance, `RBA_STORAGE_BACKEND` must be
  `s3`. `local` keeps each instance's files on its own disk.
- **The CloudFront private key** and the CPIX credentials are files readable by the `rba` user
  only (`chmod 600`). `RBA_SECRETS_KEY` must be the same on every instance and kept with the
  backups: values encrypted with a lost key read as unset and must be pasted again.

---

## 6. Deploying

### 6.1 Bare metal (per instance)

```bash
sudo deploy/install.sh                       # venv, packages, frontend build, units, nginx
sudo install -o rba -g rba -m 600 backend.env /opt/rba/backend/.env
sudo -u rba /opt/rba/backend/.venv/bin/python -m playwright install chromium
sudo /opt/rba/backend/.venv/bin/python -m playwright install-deps chromium
sudo -u rba /opt/rba/backend/.venv/bin/alembic -c /opt/rba/backend/alembic.ini upgrade head
sudo systemctl restart rba-backend
```

Run the migration once, from one instance, before restarting the others on a new build.

### 6.2 Docker

```bash
docker compose up -d --build
docker compose exec backend alembic upgrade head
```

The instance needs the IAM role, which Docker containers on EC2 inherit through the instance
metadata service. Keep IMDSv2 hop limit ≥ 2 for containers.

### 6.3 Upgrading

1. Deploy the new build to one instance and run `alembic upgrade head` there.
2. Restart the remaining instances one at a time. Their running jobs are taken over by the
   others within `RBA_JOB_LEASE_S` and resumed when they come back.

This release adds `reports.storage_key`, `jobs.storage_prefix` and the lease columns. Until
`alembic upgrade head` runs, `/api/health` reports schema drift.

---

## 7. Operations

| Area | What to do |
|---|---|
| Health | LB checks `/healthz`. `/api/health` reports the database, schema drift, storage (backend, bucket, CDN, reachable), ffmpeg, Chromium and mp4decrypt. |
| Metrics | Scrape `GET /api/metrics` (Prometheus text) on each instance. Series to alert on are listed below the table. |
| Logs | JSON lines on stdout → journald (`journalctl -u rba-backend`) or Docker's json-file driver (rotated: 50 MB × 10). Ship them with the CloudWatch agent or Fluent Bit. Set `SystemMaxUse=2G` in `/etc/systemd/journald.conf`. nginx logs rotate with the distro's logrotate. |
| Backups | RDS automated backups. On a self-managed server, `deploy/backup.sh` daily from cron, with `BACKUP_S3_URI` set. S3 versioning covers stored files. Keep `RBA_SECRETS_KEY` with the backups. |
| Retention | Database: samples and polls by the retention purge, hourly. S3: the lifecycle rules. |
| Secrets | `backend/.env` (600), the CloudFront private key and CPIX credentials as files. The CASCADA session and webhook are encrypted in the database with `RBA_SECRETS_KEY`. |

Series to alert on:
- `streamlens_up` absent;
- `streamlens_archive_failures_total` increasing;
- `streamlens_event_loop_lag_max_ms > 1000` (the host can't keep up);
- `streamlens_db_pool_checked_out` near pool size;
- `streamlens_process_resident_bytes` near 6 GB.

---

## 8. Verifying a deployment

| Check | Expected |
|---|---|
| `curl https://streamlens.<domain>/healthz` | `ok` |
| `curl https://streamlens.<domain>/api/health` | `status: ok`; `checks.storage.backend = "s3"`, `ok: true`, `cdn: true`, `cdn_signed: true`; no schema drift |
| `curl http://<instance>:8080/api/metrics` | Prometheus text with `instance="<RBA_INSTANCE_ID>"` |
| Run a 2-minute realtime analysis with **Record stream** on, then stop it | Under Analysed channels: Network log, Evidence and Recording download through a `*.cloudfront.net` URL |
| In S3 | `channels/<channel>/<day>/<job>/archives/…` and `recording/full/…` exist, tagged |
| Stop an aging job from a browser pinned to another instance | The job ends `CANCELLED` within `RBA_JOB_LEASE_S / 3` |
| Stop one instance while it runs an aging job | Another instance resumes it within `RBA_JOB_LEASE_S` (logs: `resumed 1 job(s) on instance …`) |
