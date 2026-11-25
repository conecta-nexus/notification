# Notification

FastAPI entrypoint `src.main:app`. `src.notification.main:app` re-exports the
same app for compatibility. Importing either entrypoint performs no network
I/O. The application lifespan creates one asyncpg pool and session factory per
worker and disposes the engine during shutdown. Use `get_db` for request-scoped
SQLAlchemy sessions. Task #4 initializes infrastructure without creating tables.

## Configuration

Runtime environment takes precedence over the ignored service `.env` file.
An explicitly empty runtime value overrides a value from that file. An empty
`JWT_PUBLIC_KEY` selects the configured public-key file, an empty producer
secret disables that producer, and an empty `DATABASE_URL` fails validation.
See `.env.example` for defaults. Set `DATABASE_URL` to the local or deployed
PostgreSQL 16 instance and `db_notifications` database. Accepted URL schemes
are `postgres`, `postgresql` and `postgresql+asyncpg`. All use asyncpg.
Missing or invalid configuration prevents startup. A temporary database outage
does not prevent startup with valid settings.

| Variable | Default and behavior |
| --- | --- |
| `DB_POOL_SIZE` | 5 retained connections per worker |
| `DB_MAX_OVERFLOW` | 5 extra connections per worker |
| `DB_POOL_TIMEOUT` | 5 seconds waiting for a pool slot |
| `DB_POOL_RECYCLE` | 1800 seconds before replacing an older connection on checkout |
| `DB_CONNECT_TIMEOUT` | 5 seconds for asyncpg to connect |
| `DB_HEALTH_TIMEOUT` | 2 seconds for the complete readiness check |
| `DB_ECHO` | False. Bound SQL parameters are hidden |
| `APP_ENV` | `development`. Also accepts `testing` and `production` |
| `HOST`, `PORT` | `0.0.0.0`, `8000` |

Tune the combined pool budget across workers and replicas. Pre-ping detects
stale connections on checkout. `GET /` identifies the running process.
`GET /health` executes `SELECT 1`. It returns HTTP 200 with
`{"status":"healthy","database":"connected"}` or HTTP 503 with
`{"status":"unhealthy","database":"unavailable"}`. Use the health endpoint
for readiness. The service resumes responding 200 when PostgreSQL recovers.

## Local Compose and Auth public key

From the workspace root, confirm the existing Auth public PEM at
`auth/certs/jwt_public.pem`. Reuse the Compose project and files used to start
your local stack. The workspace Compose file provides the database URL. It
expects a local `notification/.env`; create it from the example only if
missing, and preserve existing local configuration.

The default RSA issuer is `https://auth.conectanexus.ufps.edu.co`. Set
`JWT_AUDIENCES` as a JSON array if overriding the defaults
`["conecta-nexus-gateway","conecta-nexus-services"]`. A nonempty
`JWT_PUBLIC_KEY` (PEM or literal `\n` separators) takes precedence over
`JWT_PUBLIC_KEY_PATH`. Notification reads the key at startup. Use the Auth
public key only, and restart Notification after changing the configured key.

This PowerShell example creates a read-only mount in a local ignored overlay.
If the workspace already mounts the same key into Notification, reuse that
configuration. Append the overlay after any existing Compose overrides.

```powershell
$overlay = '.env.local/notification-key.compose.yaml'
if (-not (Test-Path -LiteralPath 'auth/certs/jwt_public.pem' -PathType Leaf)) {
    throw 'The existing Auth public PEM is required.'
}
if (-not (Test-Path -LiteralPath 'notification/.env')) {
    Copy-Item -LiteralPath 'notification/.env.example' -Destination 'notification/.env'
}
New-Item -ItemType Directory -Force '.env.local' | Out-Null
git check-ignore -q -- $overlay
if ($LASTEXITCODE -ne 0) { throw 'The Compose overlay must be ignored.' }
if (Test-Path -LiteralPath $overlay) { throw 'Inspect the existing overlay before reuse.' }
@'
services:
  notification:
    environment:
      JWT_PUBLIC_KEY: ""
      JWT_PUBLIC_KEY_PATH: /run/conecta-keys/jwt_public.pem
    volumes:
      - type: bind
        source: ./auth/certs/jwt_public.pem
        target: /run/conecta-keys/jwt_public.pem
        read_only: true
        bind:
          create_host_path: false
'@ | Set-Content -LiteralPath $overlay -Encoding ascii
$stack = @('compose', '-f', 'compose.yaml', '-f', $overlay)
# Replace $stack with your existing project and ordered files, plus the overlay, when applicable.
docker @stack config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Invalid Compose configuration.' }
docker @stack up -d --no-deps --build notification
if ($LASTEXITCODE -ne 0) { throw 'Notification failed to start.' }
docker exec conecta-notification uv run python -c 'import src.main; print(''OK'')'
Invoke-RestMethod http://localhost:8006/health
Invoke-RestMethod http://localhost:8080/services/notification/health
```

To check the exact mounted key against the running Auth service on the trusted
Compose network, run the following. It prints only a public-key fingerprint.

```powershell
@'
import hashlib
import json
from urllib.request import urlopen
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
from src.core.config import Settings
from src.core.security import load_verification_key

def fingerprint(key):
    return hashlib.sha256(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()

key = load_verification_key(Settings())
if key is None:
    raise SystemExit("Notification has no valid verification key.")
with urlopen("http://auth:8000/api/v1/auth/certs", timeout=5) as response:
    cert = json.load(response)
if (cert["alg"], cert["kty"], cert["use"]) != ("RS256", "RSA", "sig"):
    raise SystemExit("Unexpected Auth certificate metadata.")
local = fingerprint(key)
if local != fingerprint(load_pem_public_key(cert["public_key"].encode())):
    raise SystemExit("Public keys do not match.")
print(local)
'@ | docker exec -i conecta-notification python -
```

`authenticated_user` verifies RS256 using the configured RSA public key of at
least 2048 bits. It validates the issuer, audience list, UUID subject and
token ID, timestamps, access type, email, active status, known role and
space-delimited scopes. Missing or invalid credentials return HTTP 401 with
a Bearer challenge. User checks for future endpoints build on the returned
principal. No user-facing authenticated route is part of this task.

## Domain event listener

`POST /api/v1/notifications/events` uses a proposed internal HTTP contract.
Send JSON with `type` and `payload`. Application issue #23 confirms only the
producer's listed fields and asynchronous webhook direction. The envelope,
`contacts` as a JSON object, and producer credentials require agreement with
the producer owners. No event IDs or timestamps are mandatory at this stage.

| Type | Required payload |
| --- | --- |
| `APPLICATION_CREATED` | `student_id`, `challenge_id`, `company_id` as UUIDs |
| `APPLICATION_ACCEPTED` | `application_id`, `student_id`, `company_id` as UUIDs and `contacts` as a JSON object |
| `APPLICATION_REJECTED` | `application_id`, `student_id` as UUIDs and nonempty `feedback_reason` |
| `CHALLENGE_CLOSED` | Provisional `challenge_id` UUID. Recipient discovery remains unconfirmed |

Provision separate random secrets of at least 32 characters in
`APPLICATION_EVENT_SECRET` and `CHALLENGE_EVENT_SECRET`. Use the internal
service address with a protected network path and TLS in deployment. Send
`X-Event-Producer` with `application` or `challenge` and the matching
`X-Event-Producer-Token` header. Unconfigured or invalid credentials return
401. Authenticated producers outside their event scope get 403. Invalid
payloads get 422 without echoing contact data. User Bearer tokens do not
authorize publishing. The existing public API Gateway expects user JWTs,
so producers should use the internal service address.

The dispatcher awaits the configured async handler. HTTP 200 and
`{"status":"handled","type":"..."}` mean the handler completed in this process.
The default service has no delivery handlers and returns HTTP 503 to
authenticated producers. Handler exceptions also return 503. There is no
durable inbox, recipient delivery, SMTP retry, or event deduplication yet.
Live tests use an isolated app configured with async observation handlers.

## Verification

Run this PowerShell block from the Notification repository with Docker Desktop
running. It uses a separate Docker network and publishes a random port on
loopback. The full suite fails verification if the PostgreSQL case is missing
or skipped. The block restores the shell environment and removes its resources.

```powershell
$id = 'notification-verify-' + [Guid]::NewGuid().ToString('N')
$network = $id + '-net'
$junit = Join-Path ([IO.Path]::GetTempPath()) ($id + '.xml')
$coverageFile = Join-Path ([IO.Path]::GetTempPath()) ($id + '.coverage')
$previous = @{}
foreach ($name in 'TEST_DATABASE_URL', 'POSTGRES_PASSWORD', 'COVERAGE_FILE', 'UV_PYTHON') {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$networkId = $null
$containerId = $null
try {
    $env:UV_PYTHON = '3.12'
    $env:POSTGRES_PASSWORD = [Guid]::NewGuid().ToString('N')
    $env:COVERAGE_FILE = $coverageFile
    $networkId = docker network create $network
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated test network.' }
    $dbArgs = @(
        'run', '-d', '--rm', '--name', $id, '--network', $networkId,
        '--tmpfs', '/var/lib/postgresql/data', '-p', '127.0.0.1::5432',
        '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_DB=notification_verify', 'postgres:16-alpine'
    )
    $containerId = docker @dbArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not start disposable PostgreSQL.' }
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        docker exec $containerId pg_isready -q -h 127.0.0.1 -U postgres -d notification_verify
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
    $binding = docker port $containerId 5432/tcp
    if ($LASTEXITCODE -ne 0 -or $binding -notmatch '^127\.0\.0\.1:(\d+)$') {
        throw 'Expected one loopback-only PostgreSQL port.'
    }
    $dsn = [UriBuilder]::new('postgresql', '127.0.0.1', [int]$Matches[1], 'notification_verify')
    $dsn.UserName = 'postgres'
    $dsn.Password = $env:POSTGRES_PASSWORD
    $env:TEST_DATABASE_URL = $dsn.Uri.AbsoluteUri
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw 'Dependency synchronization failed.' }
    uv run coverage run --branch --source=src -m pytest "--junitxml=$junit"
    if ($LASTEXITCODE -ne 0) { throw 'Pytest failed.' }
    [xml]$results = Get-Content -Raw -LiteralPath $junit
    $dbTest = $results.SelectNodes("//testcase[@name='test_postgresql_pool_sessions_health_timeout_and_disposal']")
    if ($dbTest.Count -ne 1 -or $results.SelectNodes('//testcase/skipped').Count -ne 0) {
        throw 'Expected a full suite with the PostgreSQL integration test and zero skips.'
    }
    uv run coverage report -m --fail-under=80
    if ($LASTEXITCODE -ne 0) { throw 'Coverage verification failed.' }
} finally {
    if ($containerId) { docker rm -f $containerId | Out-Null }
    if ($networkId) { docker network rm $networkId | Out-Null }
    foreach ($path in $junit, $coverageFile) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
```

When health returns 503, check the effective database name, the Docker network
and deployment credentials without printing the DSN. For unexpected 401s,
check the mounted public-key fingerprint and whether the credential is an Auth
access token or a producer secret for the endpoint in question. Tasks #5–#8
cover SMTP, Notification persistence, read-state endpoints and comprehensive
service tests. The Application and Challenge producers remain separate work.
