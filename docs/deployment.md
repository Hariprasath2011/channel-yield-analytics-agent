# Production Deployment & Operations Guide

## 1. System Requirements & Architecture

The Channel Yield Analytics Agent backend is a containerized, stateless FastAPI application backed by a managed PostgreSQL database.

### 1.1 Minimum Hardware & Runtime Requirements
- **Runtime**: Python 3.11+
- **Database**: PostgreSQL 15+ (e.g. AWS RDS, GCP Cloud SQL, or Render Managed PostgreSQL)
- **CPU**: 2+ vCPU per container instance
- **Memory**: 2 GB+ RAM per container instance
- **Storage**: Stateless container; ephemeral local disk (persistent data resides in PostgreSQL)

---

## 2. Environment Variables Specification

All application settings are configured via environment variables with the `CY_` prefix.

| Variable | Required in Prod | Default | Description |
| :--- | :---: | :---: | :--- |
| `CY_ENV` | Yes | `development` | Runtime environment: `development`, `testing`, `staging`, or `production`. |
| `CY_HOST` | No | `127.0.0.1` | Bind host (set to `0.0.0.0` in containerized deployments). |
| `CY_PORT` | No | `8080` | Bind port. |
| `CY_DATABASE_URL` | **Yes** | `sqlite+pysqlite:///./dev.sqlite` | SQLAlchemy connection URI. In production, **must** be `postgresql+psycopg2://...`. |
| `CY_SECRET_KEY` | **Yes** | `""` | JWT signing secret. Must have at least 32 characters (256-bit entropy). |
| `CY_ENCRYPTION_KEY` | **Yes** | `""` | Fernet key for encrypting stored OAuth credentials. |
| `CY_ACCESS_TOKEN_EXPIRE_MINUTES` | No | `30` | JWT expiration TTL in minutes (1–1440). |
| `CY_ALLOWED_ORIGINS` | **Yes** | `http://localhost:3000` | Comma-separated list of allowed origins. **Wildcard `*` is strictly disallowed in production.** |
| `CY_MAX_UPLOAD_BYTES` | No | `52428800` | Maximum upload size in bytes (default 50 MB). |
| `CY_MAX_CSV_ROWS` | No | `100000` | Maximum rows processed per entity in a single import run. |
| `CY_RATE_LIMIT_ANON` | No | `60` | Anonymous request rate limit (requests/min). |
| `CY_RATE_LIMIT_AUTHED` | No | `300` | Authenticated request rate limit (requests/min). |
| `CY_LOG_LEVEL` | No | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `CY_ZOHO_CLIENT_ID` | Optional | `""` | Zoho Recruit OAuth 2.0 Client ID. |
| `CY_ZOHO_CLIENT_SECRET`| Optional | `""` | Zoho Recruit OAuth 2.0 Client Secret. |
| `CY_ZOHO_REDIRECT_URI` | Optional | `""` | Registered OAuth callback URL (e.g. `https://api.yourdomain.com/v2/integrations/zoho/callback`). |
| `CY_ZOHO_DATA_CENTER`  | Optional | `com` | Zoho data center: `com`, `eu`, `in`, `au`, `jp`, or `ca`. |

### 2.1 Generating Cryptographic Keys
```bash
# Generate 256-bit JWT secret key:
python -c "import secrets; print(secrets.token_hex(32))"

# Generate 32-byte Fernet encryption key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 3. Defensive Startup Configuration Guards

When `CY_ENV=production`, the application executes strict startup validation via `Settings.production_guards` in `app/core/config.py`. The server process **immediately aborts** startup if:
1. `CY_DATABASE_URL` references SQLite (`sqlite://...`).
2. `CY_SECRET_KEY` is missing or shorter than 32 characters.
3. `CY_ENCRYPTION_KEY` is missing or empty.
4. `CY_ALLOWED_ORIGINS` contains the wildcard `*` while credentials are enabled.

---

## 4. Database Setup & Migration Management

### 4.1 Schema Migration Execution
Schema changes are tracked exclusively via Alembic migrations located in `migrations/versions/`.

```bash
# Execute pending database migrations:
alembic upgrade head

# Generate static SQL preview without executing (offline verification):
alembic upgrade head --sql
```

### 4.2 SQLAlchemy Connection Pooling
For multi-worker production deployments, configure connection pooling parameters to prevent database connection exhaustion:
- `pool_size`: 10 connections per worker process.
- `max_overflow`: 20 overflow connections.
- `pool_recycle`: 1800 seconds (30 minutes) to avoid stale connection dropouts from cloud firewalls.
- `pool_pre_ping`: `True` to transparently reconnect dropped connections.

With 4 Gunicorn workers, the maximum database connection footprint is `4 * (10 + 20) = 120` connections. Ensure the PostgreSQL `max_connections` parameter is configured appropriately (e.g. >= 200).

---

## 5. Production Server Process Management

Use Gunicorn with `uvicorn.workers.UvicornWorker` for production process management:

```bash
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8080 \
  --access-logfile - \
  --error-logfile - \
  --graceful-timeout 30 \
  --timeout 120
```

### Worker Sizing Formula
$$\text{Workers} = (2 \times \text{vCPU}) + 1$$

---

## 6. Observability, Health Checks, and Probes

The application provides two distinct operational probes:

### 6.1 Liveness Probe (`GET /health`)
- **Purpose**: Verifies that the FastAPI process is alive and receiving HTTP connections.
- **Dependencies**: None. Does **not** query databases or external services.
- **Expected Status**: `HTTP 200 OK`
- **Response**:
  ```json
  {
    "status": "ok",
    "service": "channel-yield-analytics-agent",
    "version": "1.0.0",
    "environment": "production"
  }
  ```

### 6.2 Readiness Probe (`GET /ready`)
- **Purpose**: Verifies that database connectivity is active and the application is ready to accept production requests.
- **Dependencies**: Executes `SELECT 1` against the primary database.
- **Healthy Response**: `HTTP 200 OK` (`{"status": "ready", "database": "connected"}`)
- **Unhealthy Response**: `HTTP 503 Service Unavailable` (`{"status": "not_ready", "database": "disconnected"}`)

### 6.3 Kubernetes / Cloud Platform Probe Configuration Example
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 10
  timeoutSeconds: 2
  failureThreshold: 3

readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 2
  failureThreshold: 2
```

---

## 7. Backup, Recovery & Disaster Recovery

### 7.1 Automated Database Backups
- **Continuous Archiving**: Enable PostgreSQL Write-Ahead Logging (WAL) archiving for Point-In-Time Recovery (PITR).
- **Daily Logical Backups**: Automated nightly snapshot using `pg_dump`:
  ```bash
  pg_dump -Fc -h "$DB_HOST" -U "$DB_USER" "$DB_NAME" | gpg -e -r ops@company.com > "backup_$(date +%Y%m%d_%H%M%S).dump.gpg"
  ```
- **Retention**: Retain daily snapshots for 30 days, monthly snapshots for 1 year in an encrypted, geographically replicated bucket.

### 7.2 Database Restoration
```bash
# Decrypt and restore snapshot into clean database:
gpg -d backup_20261014.dump.gpg > backup.dump
pg_restore -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" --clean --if-exists backup.dump

# Validate schema migration status:
alembic current
```

---

## 8. Key and Secret Rotation Procedures

### 8.1 JWT Secret Key Rotation (`CY_SECRET_KEY`)
1. Schedule a planned maintenance window (or execute during off-peak hours).
2. Generate a new secret key: `python -c "import secrets; print(secrets.token_hex(32))"`.
3. Update `CY_SECRET_KEY` in environment variables / secret manager.
4. Restart backend instances.
5. *Impact*: Active user sessions will be required to re-authenticate at `/v2/auth/token`. Machine API keys (`cyk_...`) are unaffected as they rely on database SHA-256 hashes.

### 8.2 Fernet Encryption Key Rotation (`CY_ENCRYPTION_KEY`)
Stored third-party tokens (e.g. Zoho Recruit credentials) are encrypted with Fernet. To rotate the encryption key without data loss:
1. Keep the old key (`OLD_KEY`) and generate the new key (`NEW_KEY`).
2. Run the database re-encryption script:
   ```python
   from cryptography.fernet import Fernet
   from sqlalchemy.orm import Session
   from app.persistence.database import get_db_context
   from app.persistence.models import ZohoIntegration

   old_f = Fernet(OLD_KEY.encode())
   new_f = Fernet(NEW_KEY.encode())

   with get_db_context() as db:
       integrations = db.query(ZohoIntegration).all()
       for rec in integrations:
           if rec.encrypted_access_token:
               plain = old_f.decrypt(rec.encrypted_access_token.encode()).decode()
               rec.encrypted_access_token = new_f.encrypt(plain.encode()).decode()
           if rec.encrypted_refresh_token:
               plain = old_f.decrypt(rec.encrypted_refresh_token.encode()).decode()
               rec.encrypted_refresh_token = new_f.encrypt(plain.encode()).decode()
       db.commit()
   ```
3. Update `CY_ENCRYPTION_KEY` in the environment to `NEW_KEY`.
4. Deploy the updated container configuration.

---

## 9. Third-Party Integrations Security Boundaries

### 9.1 iGentic Direct HTTP API: BLOCKED
- **Status**: **`BLOCKED`**
- **Rationale**: The iGentic Direct HTTP API tool does not support request authentication, custom headers, bearer tokens, or client credentials.
- **Operational Requirement**: Do not expose an unauthenticated proxy, secret-in-query-param route, or public bypass to accommodate iGentic. Production integration remains blocked until authenticated request signing, bearer token injection, or a private VPC interconnect is provided.

### 9.2 Zoho Recruit Integration
- **Live Sync**: Disabled by default in testing and local development.
- **Production Enablement**: Only enabled after registering an official Zoho OAuth application, entering `CY_ZOHO_CLIENT_ID` and `CY_ZOHO_CLIENT_SECRET`, and completing admin authorization via `/v2/integrations/zoho/authorize`.
