<#
  AuditLens - PowerShell launcher (via uv)

  Usage:
      pwsh .\scripts\start.ps1              # setup env + start server
      pwsh .\scripts\start.ps1 -Check       # only check dependencies
      pwsh .\scripts\start.ps1 -NoDocker   # skip docker compose (manual DB)
      pwsh .\scripts\start.ps1 -InitDb      # only apply DB migrations

  Requires: PowerShell 7+ and Python 3.11+.
#>
param(
    [switch]$Check,
    [switch]$NoDocker,
    [switch]$InitDb
)

$ErrorActionPreference = "Stop"

# --- PowerShell 7+ guard ------------------------------------------------------
# This block must use only ASCII characters so PowerShell 5.1 can parse it and
# show a helpful error before exiting.
if ($PSVersionTable.PSVersion.Major -lt 7) {
    Write-Host "[ERR] This script requires PowerShell 7+ (pwsh)." -ForegroundColor Red
    Write-Host "      Install: winget install --id Microsoft.PowerShell --source winget" -ForegroundColor Red
    Write-Host "      Then run: pwsh .\scripts\start.ps1" -ForegroundColor Red
    throw "PowerShell 7+ is required"
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location -Path $RootDir

# --- Helpers ------------------------------------------------------------------
function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Blue }
function Ok($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg) { Write-Host "[ERR]  $msg" -ForegroundColor Red }

function Test-Command($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Err "$cmd not found. $hint"
        return $false
    }
    Ok "$cmd found: $((& $cmd --version 2>&1 | Select-Object -First 1).ToString())"
    return $true
}

function Test-IsPlaceholder($value, $placeholder) {
    return [string]::IsNullOrWhiteSpace($value) -or $value -eq $placeholder -or $value -match "REPLACE"
}

# --- Read .env into process env vars -----------------------------------------
function Read-DotEnv {
    $lines = Get-Content ".env" -Encoding utf8
    foreach ($line in $lines) {
        if ($line -match "^\s*#") { continue }
        if ($line -match "^([^=]+)=(.*)$") {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

# --- Prerequisite checks ------------------------------------------------------
function Test-Prerequisites {
    Info "Checking system dependencies..."
    $ok = $true
    $ok = (Test-Command "python" "Install Python 3.11+.") -and $ok
    $ok = (Test-Command "docker" "Install Docker Desktop: https://www.docker.com/products/docker-desktop/") -and $ok
    $ok = (Test-Command "uv" ("Install uv:`n" +
        "   winget install --id astral-sh.uv`n" +
        "   or: pip install uv`n" +
        "   see: https://docs.astral.sh/uv/getting-started/installation/")) -and $ok

    if (-not $ok) { throw "Fix missing dependencies and rerun." }

    $pyVersion = (python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1).ToString()
    if ([version]$pyVersion -lt [version]"3.11") {
        throw "Python 3.11+ required. Found: $pyVersion"
    }
    Ok "Python version OK: $pyVersion"

    docker compose version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose v2 not found. Update Docker Desktop."
    }
    Ok "Docker Compose v2 found"
}

# --- .env validation ----------------------------------------------------------
function Test-Env {
    if (-not (Test-Path -Path ".env")) {
        if (-not (Test-Path -Path ".env.example")) {
            throw ".env and .env.example not found. Check the repository."
        }
        Warn ".env not found - copying from .env.example"
        Copy-Item ".env.example" ".env"
    }

    Read-DotEnv

    $llmKey = [System.Environment]::GetEnvironmentVariable("LLM_API_KEY", "Process")
    if (Test-IsPlaceholder $llmKey "fw_REPLACE_WITH_YOUR_KEY") {
        Warn "LLM_API_KEY in .env is still a placeholder. Fill it from docs/API_KEYS.md"
    }
    Ok ".env loaded"

    $dsnFromEnv = [System.Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
    if ($dsnFromEnv -and ($dsnFromEnv -match "localhost:5434/")) {
        Warn "DATABASE_URL points to port 5434. Make sure Postgres is listening on that port."
        Warn "   Standard docker compose in this repo usually exposes port 5432."
    }
}

# --- Docker (PostgreSQL + SearXNG) -------------------------------------------
function Start-DockerServices {
    Info "Starting PostgreSQL + SearXNG via docker compose..."
    docker compose up -d 2>&1 | Select-Object -Last 10

    Info "Waiting for Postgres to become healthy..."
    $ready = $false
    for ($i = 1; $i -le 30; $i++) {
        docker compose exec -T postgres pg_isready -U audit -d bank_audit 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) {
        Err "Postgres did not become ready in 60s. Container logs:"
        docker compose logs --tail=30 postgres
        throw "Postgres is not ready"
    }
    Ok "Postgres ready"
}

# --- Virtualenv + dependencies via uv ----------------------------------------
function Install-Dependencies {
    Info "Syncing uv environment and dependencies..."

    $targetPy = (python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1).ToString()
    uv python pin $targetPy 2>&1 | Out-Null

    if (-not (Test-Path ".venv")) {
        uv venv
    }

    uv pip install -e ".[local-embeddings]"
    Ok "Python dependencies installed"

    Info "Installing Playwright Chromium..."
    uv run playwright install chromium
    Ok "Playwright ready"
}

# --- DB migrations ------------------------------------------------------------
function Invoke-DbMigrations {
    Info "Applying DB migrations..."

    if (-not (Get-Command "psql" -ErrorAction SilentlyContinue)) {
        Warn "psql not found. Skipping SQL migrations."
        Warn "   Make sure migrations are already applied, or install the PostgreSQL client."
        return
    }

    $dsn = [System.Environment]::GetEnvironmentVariable("DATABASE_URL", "Process")
    if ([string]::IsNullOrWhiteSpace($dsn)) {
        $dsn = "postgresql://audit:audit@localhost:5432/bank_audit"
    }
    $psqlDsn = $dsn -replace "postgresql\+psycopg:", "postgresql:"

    $migrations = Get-ChildItem "migrations" -Filter "*.sql" | Sort-Object Name
    foreach ($mig in $migrations) {
        Info "  -> $($mig.Name)"
        psql "$psqlDsn" -f "$($mig.FullName)" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Migration $($mig.Name) failed" }
    }

    if (Test-Path "src/bank_audit/analytics/views.sql") {
        psql "$psqlDsn" -f "src/bank_audit/analytics/views.sql" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "views.sql failed" }
    }

    Ok "Migrations applied"
}

# --- Start server -------------------------------------------------------------
function Start-Application {
    Info "Starting AuditLens..."
    uv run uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
}

# ============================================================================
#  MAIN
# ============================================================================
Test-Prerequisites
Test-Env

if ($Check) {
    Info "Check mode finished. Server will not be started."
    exit 0
}

if (-not $NoDocker) {
    Start-DockerServices
}

Install-Dependencies

if ($InitDb) {
    Invoke-DbMigrations
    Info "InitDb mode finished. Server will not be started."
    exit 0
}

Invoke-DbMigrations
Start-Application
