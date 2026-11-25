# Notification

FastAPI entrypoint `src.main:app`. `src.notification.main:app` re-exports the
same app for compatibility. Importing either entrypoint performs no network
I/O. The application lifespan creates one asyncpg pool and session factory per
worker and disposes the engine during shutdown. Use `get_db` for request-scoped
SQLAlchemy sessions. Apply schema changes explicitly with Alembic.

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
$coverageJson = Join-Path ([IO.Path]::GetTempPath()) ($id + '.coverage.json')
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
    $dbTests = @(
        'test_postgresql_pool_sessions_health_timeout_and_disposal',
        'test_notification_migration_round_trip',
        'test_notification_postgresql_schema_and_index',
        'test_notification_async_persistence_and_read_state'
        'test_postgres_concurrent_reads_and_bulk'
        'test_read_and_bulk_persist_without_changing_other_columns'
        'test_commit_failure_is_sanitized_and_rolls_back'
    )
    foreach ($test in $dbTests) {
        if ($results.SelectNodes("//testcase[@name='$test']").Count -ne 1) {
            throw "Missing PostgreSQL case $test."
        }
    }
    if ($results.SelectNodes('//testcase/skipped').Count -ne 0) {
        throw 'Expected zero skips.'
    }
    uv run coverage report -m --fail-under=80
    if ($LASTEXITCODE -ne 0) { throw 'Coverage verification failed.' }
    uv run coverage json -o $coverageJson
    if ($LASTEXITCODE -ne 0) { throw 'Coverage export failed.' }
    uv run python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1],encoding=''utf-8''))[''totals'']))' $coverageJson
    if ($LASTEXITCODE -ne 0) { throw 'Could not read coverage totals.' }
    uv run python -m scripts.benchmark_notifications
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL benchmark failed to run.' }
} finally {
    if ($containerId) { docker rm -f $containerId | Out-Null }
    if ($networkId) { docker network rm $networkId | Out-Null }
    foreach ($path in $junit, $coverageFile, $coverageJson) {
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
access token or a producer secret for the endpoint in question. Tasks #5, #7
and #8 cover SMTP, the user-facing API and comprehensive service tests. The
Application and Challenge producers remain separate work.

## Notification storage

Revision `001` creates `public.notifications` in `db_notifications`.
Alembic uses the same Settings as the service and a separate, short-lived
asyncpg engine with `NullPool`. The runtime image includes the Alembic CLI.
The application lifespan does not run migrations.

| Column | PostgreSQL type | Rules |
| --- | --- | --- |
| `id` | UUID | Primary key. ORM generates UUID4 and PostgreSQL defaults to `gen_random_uuid()` |
| `user_id` | UUID | Required Auth user UUID from JWT `sub`. A user can have many rows. No cross-service foreign key |
| `event_type` | VARCHAR | Required. Supports all four current event values and future additions |
| `title` | VARCHAR | Required |
| `message` | TEXT | Required |
| `link_url` | VARCHAR | Optional. Use NULL when there is no navigation destination |
| `is_read` | BOOLEAN | Required and defaults to false in the ORM and PostgreSQL |
| `created_at` | TIMESTAMP WITH TIME ZONE | Required. ORM supplies UTC and PostgreSQL defaults to `now()`. Updating the read state preserves the original time |

The published contracts specify no VARCHAR limits. Application and frontend
code can enforce presentation limits as needed. The B-Tree index
`ix_notifications_user_id_is_read_created_at` orders columns as
`(user_id, is_read, created_at)`. PostgreSQL can scan it backward for
newest-first unread results and use its leading columns for unread counts.
Future endpoints own authorization, read-state changes and navigation. The
current event dispatcher does not automatically create records or deliver mail.

### Apply the schema

With the Notification container rebuilt, first verify that its configured
target is the intended PostgreSQL 16 `db_notifications` database and inspect
its schema and revision. Investigate unexpected tables or revision history
before applying migrations. From the workspace root:

```powershell
docker exec conecta-notification uv run alembic upgrade head
docker exec conecta-notification uv run alembic current
docker exec conecta-notification uv run alembic heads
docker exec conecta-notification uv run alembic check
```

Alembic also supports `history` and offline `upgrade head --sql`.
**Downgrading revision 001 drops the notifications table and its history.**
Test `downgrade base` only with disposable data. Use a backup and a separately
reviewed recovery procedure for any production rollback that must retain rows.

### Verify index performance

The PowerShell verification block runs `scripts.benchmark_notifications` on
the disposable database after the migration suite. The benchmark inserts
120,000 synthetic rows across 1,010 recipients, including ten with 2,000 rows
and a thousand with 100 rows. One third of rows are read. It runs `ANALYZE`
and captures JSON plans for a page of 20 unread rows and the unread count.
It measures an initial query after loading, followed by 30 repeats per
recipient without forcing the planner, and rolls back its fixture.

The results are database execution times. Loading the dataset and checking
cardinalities may already warm shared buffers, and the measured times exclude
network, client and HTTP latency. They characterize the test dataset and
environment.

On the local PostgreSQL 16.15 run, the normal planner chose the compound
index for all 155 page queries. The 150 repeated queries took at most
0.183 ms, with per-recipient p95 from 0.046 to 0.130 ms. The five initial
page queries took 0.036 to 0.112 ms.

## Transactional email

Use `await EmailDispatcher(settings).send(context)` with a typed
`CreatedEmail`, `AcceptedEmail`, or `RejectedEmail` from
`src/services/email_rendering.py`. Each context requires one recipient
mailbox and a human-readable challenge title. Creation goes to the company
and can include the authorized student display name. Acceptance uses a
separate student and company context, each with only its counterparty's
authorized name and at least one email or phone. Rejection goes to the
student and includes optional feedback. Supply a per-recipient UUID as
`delivery_id` when a caller has stable delivery identity.

Templates use Jinja strict undefined handling and HTML autoescaping. MIME
has UTF-8 plain text and HTML alternatives with a single To header. The
wordmark and palette reproduce the project styling documented in
`frontend/DESIGN.md` and `frontend/app/components/landing/LandingNavbar.vue`.
The navbar defines a text wordmark with a crimson dot. There is no separate
approved illustration in that component. The email uses the same text
treatment and does not need image downloads or a tracking pixel.
Buttons use the configured origin and fixed existing paths `/challenges/manage`,
`/me`, or `/challenges`. Application detail navigation awaits a confirmed route.

### SMTP settings

| Variable | Contract |
| --- | --- |
| `SMTP_HOST` | Empty disables delivery. Use `127.0.0.1` for host-local Mailpit |
| `SMTP_PORT` | Defaults to `1025`. Configure the actual deployed SMTP port |
| `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME` | Sender mailbox and display name. The name defaults to Conecta Nexus |
| `SMTP_TLS_MODE` | `plain` for local unauthenticated Mailpit, `starttls` for mandatory upgrade, or `tls` for implicit TLS |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | Optional pair. Authentication requires TLS |
| `SMTP_TIMEOUT` | Total attempt deadline in seconds, default `10` |
| `SMTP_RETRY_BASE_DELAY` | First retry delay in seconds, default `1` |
| `FRONTEND_BASE_URL` | Trusted HTTP(S) origin. Paths, credentials, query strings and fragments are rejected |

Production delivery requires TLS and certificate verification. Configure
the deployed SMTP host and port explicitly. Runtime empty optional values
override dotenv. Disabled SMTP preserves normal application health and
raises a typed delivery failure when invoked. No shared SMTP variables are
needed for the verification harness.

The dispatcher returns a receipt only after SMTP accepts DATA. It retries
connection failures before DATA and explicit temporary SMTP replies, with
at most three total attempts and base/double-base delays. Permanent replies
and certificate failures terminate immediately. A disconnect or timeout
during DATA is ambiguous and is never retried automatically. One operation
keeps the same Message-ID. Logs and exceptions expose only the failure
class and attempt count. A Message-ID provides correlation and does not
deduplicate across events.

`application_email_handlers` connects an explicitly authorized,
asynchronous context resolver to the existing EventDispatcher. An accepted
event attempts each recipient once per operation, preserving partial
results without repeating the successful recipient inside that operation.
There is no production resolver and no persistent delivery ledger. The root
Application producer, recipient lookup and contacts contract remain
external dependencies. Do not automatically replay partial or ambiguous
operations until durable recipient-level reconciliation exists.

### Verify delivery with disposable Mailpit

From the Notification repository, run this PowerShell block with ports
1025 and 8025 available. It binds both to loopback on an isolated Docker
network and removes its own resources. The harness creates its own
temporary authorized HTTP producer and uses only synthetic addresses.
The output directory contains the SMTP-captured MIME, HTML and plain text.

```powershell
$name = 'notification-mailpit-' + [Guid]::NewGuid().ToString('N')
$network = $name + '-net'
$output = Join-Path ([IO.Path]::GetTempPath()) $name
$networkId = $null
$container = $null
try {
    $networkId = docker network create $network
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Mailpit network.' }
    $mailpitArgs = @(
        'run', '-d', '--rm', '--name', $name, '--network', $networkId,
        '-p', '127.0.0.1:1025:1025', '-p', '127.0.0.1:8025:8025',
        'axllent/mailpit@sha256:c076638db1e15662150be4fb62b8a6e96ef6ba5bde90c838a0239225854830f7'
    )
    $container = docker @mailpitArgs
    if ($LASTEXITCODE -ne 0) { throw 'Mailpit failed to start. Check the port owner before retrying.' }
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $ready = (Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8025/' -TimeoutSec 2).StatusCode -eq 200
            if ($ready) { break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'Mailpit UI did not become ready.' }
    uv run --locked python -m scripts.verify_email $output
    if ($LASTEXITCODE -ne 0) { throw 'Mailpit verification failed.' }
    Write-Output "Captured evidence: $output"
} finally {
    if ($container) { docker rm -f $container | Out-Null }
    if ($networkId) { docker network rm $networkId | Out-Null }
}
```

The UI is at `http://127.0.0.1:8025/` while the container is running. Open
individual captured messages to inspect the HTML, plain text and source.
The harness uses the [Mailpit v1 API](https://mailpit.axllent.org/docs/api-v1/)
to collect only the fixture Message-IDs, confirm recovery after a connection
closed before the SMTP greeting, and verify that terminal failure produces
no captured message.

Run the full suite using the disposable PostgreSQL procedure above. For
the official selector, from the workspace root run:

```powershell
$venv = '/tmp/email-pytest-' + [Guid]::NewGuid().ToString('N')
try {
    docker exec -e UV_NO_DEV=0 -e "UV_PROJECT_ENVIRONMENT=$venv" conecta-notification uv run --dev --locked pytest tests/test_email_service.py
    if ($LASTEXITCODE -ne 0) { throw 'Email pytest selector failed.' }
} finally {
    docker exec conecta-notification python -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' $venv
}
```

## User notification API

Use an Auth-issued Bearer access token. Every operation uses the verified JWT
subject as the recipient UUID, including for administrators. Gateway's
diagnostic proxy does not bypass this verification. Responses from the routes
use `Cache-Control: no-store`.

| Method | Canonical path | Request |
| --- | --- | --- |
| GET | `/api/v1/notifications/me` | `limit` defaults to 50 (1–100). `offset` defaults to 0 (0–2147483647) |
| PATCH | `/api/v1/notifications/{notification_id}/read` | UUID path parameter. No body required. An empty JSON object or null also works |
| PATCH | `/api/v1/notifications/read-all` | No body required. An empty JSON object or null also works |

In Nuxt's `useApi().apiFetch`, pass `/notifications/me`,
`/notifications/${id}/read`, or `/notifications/read-all`. The composable
supplies `/api/v1` and Authorization. Poll GET `/me` for the badge and use
the returned `unread_count` after each PATCH.

```json
{
  "items": [
    {
      "id": "f9326796-61ae-4e88-8893-6d893b44897d",
      "event_type": "APPLICATION_ACCEPTED",
      "title": "Postulación aceptada",
      "message": "Tu postulación ha sido aceptada.",
      "link_url": "/challenges",
      "is_read": false,
      "created_at": "2026-11-25T22:00:00Z"
    }
  ],
  "unread_count": 1
}
```

`event_type` is the stored string. IDs are UUID strings and times use ISO 8601
UTC. Links are root-relative printable ASCII paths. Encoded paths, dot
segments, queries, fragments, backslashes and external or executable
destinations are returned as null. The stored value stays intact. The frontend
should still allow only supported same-origin destinations before navigating.

The list orders by `created_at DESC, id DESC`. Advance the offset by the
number of items returned, and stop when a page returns fewer than the requested
limit. An empty page returns `items: []`, while the unread count always covers
the entire owner history. The page and count share a PostgreSQL statement
snapshot. Offset positions can shift between requests when rows are inserted.

Single PATCH returns
`{"id":"f9326796-61ae-4e88-8893-6d893b44897d","is_read":true,"unread_count":0}`.
Bulk PATCH returns `{"updated_count":1,"unread_count":0}`. Only unread rows
transition. Repeating bulk PATCH returns zero newly updated rows once no owned
unread rows remain. Writes commit before success. Their count is computed in a
second statement inside the same READ COMMITTED transaction. A notification
committed by another transaction before that statement can produce a positive
count after bulk read. Arrivals after that snapshot appear on later requests.

Errors use `{"detail":"..."}`. Missing or invalid authentication returns 401
with a Bearer challenge at Notification. An unknown or foreign UUID returns
the same 404. Invalid UUIDs, out-of-range pagination and PATCH payload fields
return 422. Recognized connectivity failures return 503, and unexpected
database failures return a generic 500. Failed transactions roll back.

`GET /openapi.json` and `/docs` on the direct service describe the API.
The disposable PostgreSQL verification above runs the API, inherited tests,
and migration checks with actual PostgreSQL and Alembic. The API fixtures
seed records using the real model and clean only test-created users' rows.
For live Gateway checks, use Auth-issued synthetic identities and the same
Bearer requests at ports 8006 and 8080. Use POST `/api/v1/auth/login`,
verify the token with GET `/api/v1/auth/verify`, and keep credentials and
tokens out of recorded output. Any committed shared smoke-test rows must be
tracked and removed by exact synthetic UUID. The storage model has no new
migration. Delivery from real Application or Challenge publishers and
frontend integration remain separate tasks.

### Official router test selector in Docker

Use a disposable PostgreSQL 16 database named `notification_verify` and a
unique hostname reachable by `conecta-notification`. Keep any test database
on a separate network for the full host suite. For the in-container selector,
if attaching the disposable database to the shared network, give it a
unique container name and never advertise the `postgres` hostname.

From the workspace root, run the following with a disposable DSN in
`TEST_DATABASE_URL`. Docker forwards its value through the environment.
The isolated test virtual environment leaves the runtime dependencies intact.

```powershell
if (-not $env:TEST_DATABASE_URL) { throw 'Provide a disposable test database.' }
$venv = '/tmp/notify-api-pytest-' + [Guid]::NewGuid().ToString('N')
try {
    docker exec -e TEST_DATABASE_URL -e UV_NO_DEV=0 -e "UV_PROJECT_ENVIRONMENT=$venv" conecta-notification uv run --dev --locked pytest tests/test_notification_router.py
    if ($LASTEXITCODE -ne 0) { throw 'Notification router selector failed.' }
} finally {
    docker exec conecta-notification python -c 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)' $venv
}
```

Confirm the selector reports zero skips. Remove only the disposable database
container and restore the shell variable afterward.

## Comprehensive verification

The suite covers configuration, RSA authentication and producer authorization,
PostgreSQL models and Alembic migrations, MIME rendering and SMTP behavior,
and the authenticated notification API. SMTP unit tests replace only the
transport and parse the bytes passed to `DATA` for all four recipient
variants. SQL tests use PostgreSQL 16. Router error classification is tested
separately from real outages, and concurrent write tests use real
transactions. One test waits between the bulk update and count so that a
concurrent insert commits before the count snapshot.

From the Notification repository, execute these PowerShell commands with
Docker Desktop running. The host mode locks dependencies, exports collection,
runs plain pytest and repeats under branch coverage. Each execution starts
with its own empty `notification_verify` database. Reports include JUnit,
coverage JSON, the human-readable coverage table, and synthetic MIME
captured at the transport boundary.

```powershell
$verify = [ScriptBlock]::Create((Get-Content -Raw -Encoding UTF8 -LiteralPath 'scripts/verify_tests.ps1'))
& $verify -Mode Host -ReportDirectory '../.env.local/notification-task-05/review/final'
& $verify -Mode Docker -ReportDirectory '../.env.local/notification-task-05/review/docker'
```

Docker mode runs the official complete `pytest -q` inside the existing
`conecta-notification` container. It uses a unique temporary uv environment
and a disposable database with a unique hostname on the shared network.
It checks shared `postgres` resolution, removes the temporary resources and
restores shell variables. The script requires zero omitted tests and an 80%
combined coverage threshold for all of `src`. Runtime dependencies and
migrations remain unchanged.

For real mail delivery use the disposable Mailpit block above. For live HTTP
regression use real Auth-issued tokens and the documented direct and Gateway
routes. Persist test fixtures through the model, then remove only their exact
synthetic UUIDs. These checks exercise separate boundaries and report their
evidence separately from mocks. Actual producer emission, authorized
recipient resolution, automatic event storage, durable event idempotency,
production SMTP, the frontend drawer and Cloud Run acceptance require
separate integration.

The recorded baseline passed 90 tests. The final suite passed 127 and covered
599 of 608 statements and 86 of 92 branches. On the recorded run,
combined coverage rose from 92.71% to 97.86%, statement coverage from
94.74% to 98.52%, and branch coverage from 79.35% to 93.48%. The query
module reached 100%. The router measured 88% combined. Its 404, sanitized
500 and temporary-storage 503 behavior is also directly asserted at the
HTTP boundary. Refer to the review's per-module report for remaining
coverage gaps and the tracer comparison.
