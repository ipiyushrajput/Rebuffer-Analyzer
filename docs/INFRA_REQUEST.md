# StreamLens — infrastructure request

What the StreamLens team needs provisioned for production, and the values to send back. The
reasoning and the full configuration are in
[`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md).

StreamLens is a Python (FastAPI) backend plus a static React frontend served by nginx, under
`/streamlens/`. The two can run on one machine or on separate ones (§0). It analyses live HLS
channels:
- several identical instances run behind a load balancer;
- they share one MySQL database;
- they write their files to S3, which users download through CloudFront.

---

## 0. Ports and paths

StreamLens lives under **`/streamlens/`** on the frontend's host name, e.g.
`https://dev.samsungtv/streamlens/`. It never claims `/`, so it shares a host with other sites.
The browser talks only to the frontend machine. nginx there serves the page and forwards
`/streamlens/api/` and `/streamlens/ws/` to the backend machine, stripping the `/streamlens`
prefix on the way.

```
Browser ──HTTPS 443 / HTTP 8080──▶ Frontend machine (nginx) ──TCP 8010──▶ Backend machine (uvicorn)
                                                                              │
                                                                              ├─TCP 3306──▶ RDS MySQL
                                                                              └─TCP 80/443─▶ internet
```

### Firewall / security group rules

| # | From | To | Port | Purpose |
|---|---|---|---|---|
| 1 | Users (office / VPN range) | Frontend machine | TCP **443** (an existing HTTPS site) or **8080** (the StreamLens site) | The web UI |
| 2 | **Frontend machine**, its private IP only | **Backend machine** | TCP **8010** inbound on the backend | nginx forwarding API and WebSocket traffic |
| 3 | Backend machine | RDS (`remotelab.czgcvnost4da.us-west-2.rds.amazonaws.com`) | TCP **3306** | Database |
| 4 | Backend machine | Internet, any host | TCP **443** and **80** | Channel CDNs, ad servers, `cascada.samsungcloud.tv`, KeyOS, Widevine licence server, TV Plus catalogue, S3 |
| 5 | Both machines | DNS resolver | 53 | Name resolution |
| 6 | Administrators | Both machines | SSH 22 / SSM | Administration |

- Port **8010** on the backend is reached by the frontend machine only, never by users.
- Port **8001** stays Metanalyser's on both machines.
- **Long-lived connections:** WebSockets on `/streamlens/ws/` stay open up to 24 hours, and report
  downloads up to 1 hour. Anything between the two machines (firewall, security group, load
  balancer) needs an idle timeout of at least 1 hour and must pass the WebSocket `Upgrade`
  header.
- On one machine, rule 2 is not needed: nginx reaches the backend on `127.0.0.1:8010`.

### Paths on the frontend machine (what users and load balancers call)

| Path | Answered by | What it is |
|---|---|---|
| `/streamlens/` | nginx | The web UI (every path below it that is not one of the following) |
| `/streamlens` | nginx | Redirects to `/streamlens/` |
| `/streamlens/healthz` | nginx | Liveness of the frontend: the load balancer health check (`200 ok`) |
| `/streamlens/health` | backend `/api/health` | Health of the backend, its database and storage, as JSON |
| `/streamlens/api/*` | backend `/api/*` | Every API call, report and evidence download, the stream proxy, the DRM licence relay |
| `/streamlens/api/metrics` | backend `/api/metrics` | Prometheus metrics; restrict to the monitoring network |
| `/streamlens/ws/realtime/{id}` | backend `/ws/realtime/{id}` | WebSocket: live analysis progress |
| `/streamlens/ws/jobs/{id}` | backend `/ws/jobs/{id}` | WebSocket: aging and bulk job progress |
| `/healthz` | nginx | The StreamLens site on 8080 only: health check on that port |

### Paths on the backend machine (port 8010, what nginx calls)

`/api/*` (including `/api/health` and `/api/metrics`) and `/ws/realtime/{id}`, `/ws/jobs/{id}`.
The backend has no `/streamlens` prefix; nginx removes it.

### nginx on the frontend machine

The StreamLens locations are one file, `/etc/nginx/snippets/streamlens.conf`, rendered by
`deploy/install.sh --role frontend --backend <backend-ip>:8010`. Either:
- the installer's own site serves it on **8080**; or
- with `--snippet-only`, the team that owns the existing site (e.g. `https://dev.samsungtv`) adds
  one line inside that site's `server { }` block:

```
include /etc/nginx/snippets/streamlens.conf;
```

---

## 1. Compute — 2 instances (scalable to more)

| Item | Requirement |
|---|---|
| Type | EC2 (or ECS on EC2). 8 vCPU / 16 GB RAM each (e.g. `c6i.2xlarge` / `m6i.2xlarge`) |
| OS | Ubuntu 24.04 LTS or 22.04 LTS, x86-64 (or Docker Engine 24+ with Compose v2) |
| Disk | 100 GB gp3 root (recordings are spooled here only while a run is in progress) |
| IAM | The instance profile from §5 |
| Time | NTP / Amazon Time Sync enabled |
| Inbound | Frontend: TCP 8080 (or the existing site's 443) from the load balancer / users. Backend: TCP 8010 from the frontend machine only. SSH/SSM for administration. **Port 8001 must stay free** (Metanalyser). |
| Outbound | Unrestricted HTTPS/HTTP to the internet (see §8), S3 and the database |
| Packages | Python 3.11+, Node 22, nginx, ffmpeg, rsync, MySQL client — `deploy/prereqs-ubuntu.sh` installs them. `deploy/install.sh` installs the application. |

## 2. Load balancer (ALB)

| Item | Requirement |
|---|---|
| Listener | HTTPS 443 with an ACM certificate for the hostname in §9, HTTP → HTTPS redirect |
| Target group | The instances on port **8080** (HTTP) |
| Health check | `GET /streamlens/healthz` → `200`, interval 15 s |
| Stickiness | **Enabled**, LB-generated cookie, duration 1 day (required: realtime analysis is pinned to the instance running it) |
| Idle timeout | **3600 s** (WebSocket progress streams on `/ws/*`) |
| Access | Office / VPN CIDRs only, or WAF IP allowlist |
| Rule | Deny `/streamlens/api/metrics` from outside the monitoring network |

## 3. Database — MySQL 8.0 (RDS)

| Item | Requirement |
|---|---|
| Engine | MySQL 8.0, Multi-AZ, `db.r6g.large` or larger |
| Storage | 200 GB gp3, autoscaling on |
| Parameter group | `max_allowed_packet = 268435456`, `time_zone = UTC`, `wait_timeout = 28800`, `max_connections ≥ 200` |
| Backups | Automated, 14-day retention, point-in-time recovery |
| Network | Private subnets; security group allows 3306 from the instances only |
| Schema/user | Database `rba` (`utf8mb4_unicode_ci`); user `rba` with `ALL PRIVILEGES ON rba.*` |

## 4. Object storage — S3 bucket

| Item | Requirement |
|---|---|
| Bucket | One private bucket, e.g. `streamlens-prod-<account>`, in the instances' region |
| Public access | Block all public access (all four settings) |
| Encryption | SSE-S3 (AES-256) default encryption. If SSE-KMS is mandated, tell us the key ARN. |
| Versioning | Enabled |
| Bucket policy | Read for the CloudFront distribution's Origin Access Control only |
| Lifecycle rules | 1. Tag `retention=standard` → expire after **30 days**. 2. Tag `retention=pinned` → expire after **180 days**. 3. Tag `retention=keep` → no expiry (Glacier IR after 90 days is fine). 4. Abort incomplete multipart uploads after **7 days**. |
| Expected volume | Reports ~1 MB each. Network logs 1–50 MB per run. Recordings up to several GB per realtime run (30-day expiry). |

## 5. IAM role for the instances

Instance profile `streamlens-instance-role`, with no access keys anywhere:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Objects",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject", "s3:PutObjectTagging", "s3:GetObject", "s3:GetObjectTagging",
        "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::<bucket>/<prefix>*"
    },
    {
      "Sid": "List",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<bucket>",
      "Condition": { "StringLike": { "s3:prefix": ["<prefix>*"] } }
    }
  ]
}
```

Also attach `AmazonSSMManagedInstanceCore` (administration) and `CloudWatchAgentServerPolicy`
(logs and metrics). Add `kms:GenerateDataKey` and `kms:Decrypt` if the bucket uses SSE-KMS.

## 6. CloudFront

| Item | Requirement |
|---|---|
| Distribution | Origin = the S3 bucket with **Origin Access Control** |
| Viewer protocol | Redirect HTTP to HTTPS |
| Viewer access | **Restricted with a trusted key group** (signed URLs) |
| Key pair | An RSA 2048 key pair: public key in the key group; **private key PEM** delivered to each instance at `/etc/streamlens/cloudfront-private-key.pem` (owner `rba`, mode `600`), e.g. from Secrets Manager at boot |
| Cache policy | CachingOptimized (objects never change once written) |
| Optional | Custom domain (e.g. `files.streamlens.<domain>`) with an ACM certificate in us-east-1; WAF IP allowlist |

## 7. Monitoring and logs

| Item | Requirement |
|---|---|
| Metrics | Prometheus (or CloudWatch agent with Prometheus scraping) scraping `http://<instance>:8080/streamlens/api/metrics` (or the backend's `:8010/api/metrics`) every 30 s |
| Alerts | Missing `streamlens_up`; rising `streamlens_archive_failures_total`; `streamlens_event_loop_lag_max_ms > 1000` for 5 min; `streamlens_process_resident_bytes > 5.5e9`; ALB unhealthy hosts; RDS CPU / connections / storage |
| Logs | CloudWatch agent shipping journald unit `rba-backend` and `/var/log/nginx/rba-*.log`; 30-day retention |

## 8. Outbound network from the instances

| Destination | Purpose |
|---|---|
| Any HTTPS/HTTP host | The channels' origins, CDNs and ad servers (a channel can be on any CDN) |
| `cascada.samsungcloud.tv` | Field rebuffering metrics |
| KeyOS CPIX endpoint, Widevine licence server | Protected channels |
| S3 (gateway VPC endpoint recommended) | Storage |
| PyPI, the npm registry, and Playwright's browser download host (`playwright install chromium`) | Installation and upgrades only |
| DNS (VPC resolver) | Name resolution, also measured by the analysis |

## 9. Values to send back to the StreamLens team

| Value | Example |
|---|---|
| Application URL (ALB hostname / DNS) | `https://dev.samsungtv/streamlens/` |
| Backend machine private IP (when apart) | `10.0.0.5` — what `--backend` is given on the frontend machine |
| S3 bucket name, region, prefix | `streamlens-prod-1234`, `us-east-1`, `prod` |
| CloudFront domain | `https://d1abcd.cloudfront.net` (or the custom domain) |
| CloudFront public key ID (key group) | `K2ABCDEFGHIJKL` |
| Path of the private key on the instances | `/etc/streamlens/cloudfront-private-key.pem` |
| RDS endpoint, port, database, user | `streamlens.xxxx.rds.amazonaws.com`, `3306`, `rba`, `rba` |
| RDS password | Through Secrets Manager / a secure channel, never email or chat |
| Instance names (one `RBA_INSTANCE_ID` each) | `streamlens-a`, `streamlens-b` |
| KMS key ARN | Only if SSE-KMS is used |
| Prometheus / CloudWatch target | So we can confirm scraping |

The application team then fills `backend/.env` on each instance (§5 of the deployment guide),
runs `deploy/install.sh` and `alembic upgrade head`, and verifies with the checklist in §8 of
the deployment guide.
