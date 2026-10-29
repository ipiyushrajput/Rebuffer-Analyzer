# StreamLens — infrastructure request

What the StreamLens team needs provisioned for production, and the values to send back. The
reasoning and the full configuration are in
[`PRODUCTION_DEPLOYMENT.md`](PRODUCTION_DEPLOYMENT.md).

StreamLens is a Python (FastAPI) backend plus a static React frontend served by nginx. It
analyses live HLS channels:
- several identical instances run behind a load balancer;
- they share one MySQL database;
- they write their files to S3, which users download through CloudFront.

---

## 1. Compute — 2 instances (scalable to more)

| Item | Requirement |
|---|---|
| Type | EC2 (or ECS on EC2). 8 vCPU / 16 GB RAM each (e.g. `c6i.2xlarge` / `m6i.2xlarge`) |
| OS | Ubuntu 24.04 LTS or 22.04 LTS, x86-64 (or Docker Engine 24+ with Compose v2) |
| Disk | 100 GB gp3 root (recordings are spooled here only while a run is in progress) |
| IAM | The instance profile from §5 |
| Time | NTP / Amazon Time Sync enabled |
| Inbound | TCP 8080 from the load balancer only. SSH/SSM for administration. **Port 8001 must stay free** (Metanalyser). |
| Outbound | Unrestricted HTTPS/HTTP to the internet (see §8), S3 and the database |
| Packages | Python 3.11, Node 22, nginx, ffmpeg, rsync, MySQL client. `deploy/install.sh` installs the application. |

## 2. Load balancer (ALB)

| Item | Requirement |
|---|---|
| Listener | HTTPS 443 with an ACM certificate for the hostname in §9, HTTP → HTTPS redirect |
| Target group | The instances on port **8080** (HTTP) |
| Health check | `GET /healthz` → `200`, interval 15 s |
| Stickiness | **Enabled**, LB-generated cookie, duration 1 day (required: realtime analysis is pinned to the instance running it) |
| Idle timeout | **3600 s** (WebSocket progress streams on `/ws/*`) |
| Access | Office / VPN CIDRs only, or WAF IP allowlist |
| Rule | Deny `/api/metrics` from outside the monitoring network |

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
| Metrics | Prometheus (or CloudWatch agent with Prometheus scraping) scraping `http://<instance>:8080/api/metrics` every 30 s |
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
| Application URL (ALB hostname / DNS) | `https://streamlens.example.com` |
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
