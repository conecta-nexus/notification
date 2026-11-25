param(
    [Parameter(Mandatory = $true)][string]$ReportDirectory,
    [ValidateSet('Host', 'Docker')][string]$Mode = 'Host'
)

if (-not (Test-Path -LiteralPath 'src/main.py')) { throw 'Run from the Notification repository root.' }
$reports = (New-Item -ItemType Directory -Force $ReportDirectory).FullName
$variables = @('TEST_DATABASE_URL', 'POSTGRES_PASSWORD', 'COVERAGE_FILE', 'UV_PYTHON', 'NOTIFICATION_TEST_MIME_DIR')
$previous = @{}
foreach ($name in $variables) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE." }
}

function Assert-JUnit {
    param([string]$Path)
    [xml]$junit = Get-Content -Raw -LiteralPath $Path
    if ($junit.SelectNodes('//testcase/error | //testcase/failure | //testcase/skipped').Count) {
        throw 'Full verification requires zero failed or omitted tests.'
    }
    foreach ($test in @(
        'test_notification_migration_round_trip',
        'test_postgres_concurrent_reads_and_bulk',
        'test_arrival_between_bulk_update_and_count_remains_unread'
    )) {
        if ($junit.SelectNodes("//testcase[@name='$test']").Count -ne 1) {
            throw "Required PostgreSQL test not collected: $test"
        }
    }
}

function Invoke-FreshSuite {
    param([ValidateSet('plain', 'coverage', 'docker')][string]$Run)
    $id = 'notification-verify-' + [Guid]::NewGuid().ToString('N')
    $networkId = $null
    $container = $null
    $coveragePath = $null
    $venv = "/tmp/$id"
    try {
        $env:POSTGRES_PASSWORD = [Guid]::NewGuid().ToString('N')
        if ($Run -eq 'docker') {
            $attached = Invoke-Checked docker @('inspect', '--format', '{{json .NetworkSettings.Networks}}', 'conecta-notification')
            $networks = @(($attached | ConvertFrom-Json).PSObject.Properties.Name)
            if ($networks.Count -ne 1) { throw 'Expected one known network on conecta-notification.' }
            $network = $networks[0]
        } else {
            $network = $id + '-net'
            $networkId = Invoke-Checked docker @('network', 'create', $network)
        }
        $argsForDatabase = @(
            'run', '-d', '--rm', '--name', $id, '--network', $network,
            '--tmpfs', '/var/lib/postgresql/data', '-e', 'POSTGRES_PASSWORD',
            '-e', 'POSTGRES_DB=notification_verify'
        )
        if ($Run -ne 'docker') { $argsForDatabase += @('-p', '127.0.0.1::5432') }
        $container = Invoke-Checked docker ($argsForDatabase + 'postgres:16-alpine')
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            docker exec $container pg_isready -q -h 127.0.0.1 -U postgres -d notification_verify
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw 'Disposable PostgreSQL did not become ready.' }
        $sql = "SELECT current_database(), current_setting('server_version'), (SELECT count(*) FROM pg_tables WHERE schemaname='public')"
        $identity = Invoke-Checked docker @('exec', $container, 'psql', '-U', 'postgres', '-d', 'notification_verify', '-XAtc', $sql)
        if ($identity -notmatch '^notification_verify\|16\.[^|]+\|0$') { throw 'Unexpected test database identity or schema.' }
        Write-Output "$Run database $identity"
        if ($Run -eq 'docker') {
            $metadata = Invoke-Checked docker @('inspect', '--format', '{{json .NetworkSettings.Networks}}', $container)
            if (($metadata | ConvertFrom-Json).$network.Aliases -contains 'postgres') {
                throw 'Disposable container must never publish the shared postgres alias.'
            }
            $sharedMetadata = Invoke-Checked docker @('inspect', '--format', '{{json .NetworkSettings.Networks}}', 'conecta-postgres')
            $resolvePostgres = "import socket; print(','.join(sorted(set(socket.gethostbyname_ex('postgres')[2]))))"
            $actual = Invoke-Checked docker @('exec', 'conecta-notification', 'python', '-c', $resolvePostgres)
            if ($actual -ne ($sharedMetadata | ConvertFrom-Json).$network.IPAddress) {
                throw 'Shared postgres DNS changed.'
            }
            $hostname = $id
            $port = 5432
        } else {
            $binding = Invoke-Checked docker @('port', $container, '5432/tcp')
            if ($binding -notmatch '^127\.0\.0\.1:(\d+)$') { throw 'Expected a loopback-only port.' }
            $hostname = '127.0.0.1'
            $port = [int]$Matches[1]
        }
        $uri = [UriBuilder]::new('postgresql', $hostname, $port, 'notification_verify')
        $uri.UserName = 'postgres'
        $uri.Password = $env:POSTGRES_PASSWORD
        $env:TEST_DATABASE_URL = $uri.Uri.AbsoluteUri
        $junit = Join-Path $reports "$Run-junit.xml"
        if ($Run -eq 'docker') {
            $inContainer = "$venv/official-junit.xml"
            Invoke-Checked docker @(
                'exec', '-e', 'TEST_DATABASE_URL', '-e', 'UV_NO_DEV=0', '-e',
                "UV_PROJECT_ENVIRONMENT=$venv", 'conecta-notification', 'uv', 'run',
                '--dev', '--locked', 'pytest', '-q', "--junitxml=$inContainer"
            )
            Invoke-Checked docker @('cp', "conecta-notification:${inContainer}", $junit)
        } elseif ($Run -eq 'plain') {
            Invoke-Checked uv @('run', 'pytest', '-q', "--junitxml=$junit")
        } else {
            $coveragePath = Join-Path ([IO.Path]::GetTempPath()) "$id.coverage"
            $env:COVERAGE_FILE = $coveragePath
            $env:NOTIFICATION_TEST_MIME_DIR = Join-Path $reports 'mocked-smtp'
            Invoke-Checked uv @('run', 'coverage', 'run', '--branch', '--source=src', '-m', 'pytest', '-q', "--junitxml=$junit")
            $report = Invoke-Checked uv @('run', 'coverage', 'report', '-m', '--fail-under=80')
            $report | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $reports 'coverage.txt')
            $report
            Invoke-Checked uv @('run', 'coverage', 'json', '-o', (Join-Path $reports 'coverage.json'))
        }
        Assert-JUnit $junit
    } finally {
        if ($Run -eq 'docker') {
            $cleanup = 'import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)'
            docker exec conecta-notification python -c $cleanup $venv | Out-Null
        }
        if ($container) { docker rm -f $container | Out-Null }
        if ($networkId) { docker network rm $networkId | Out-Null }
        if ($coveragePath -and (Test-Path -LiteralPath $coveragePath)) {
            Remove-Item -LiteralPath $coveragePath
        }
    }
}

try {
    if ($Mode -eq 'Host') {
        $env:UV_PYTHON = '3.12'
        Invoke-Checked uv @('sync', '--locked')
        Invoke-Checked uv @('run', 'pytest', '--collect-only', '-q') |
            Set-Content -Encoding UTF8 -LiteralPath (Join-Path $reports 'collection.txt')
        Invoke-FreshSuite plain
        Invoke-FreshSuite coverage
    } else {
        Invoke-FreshSuite docker
    }
} finally {
    foreach ($name in $variables) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
