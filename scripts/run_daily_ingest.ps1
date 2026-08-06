# Daily disclosure ingest (free local runner).
# Loads .env from repo root, runs house+senate via Horizons API.
# Exit codes: 0 ok, 1 missing config, 2 python failure

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogFile = Join-Path $LogDir "ingest_$stamp.log"
$LatestLog = Join-Path $LogDir "latest.log"

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line
    Write-Host $line
}

Write-Log "=== rollcalltrack daily ingest start ==="
Write-Log "repo=$RepoRoot"

# Load .env (KEY=VALUE, ignore comments/blank)
$EnvFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Log "ERROR: missing .env — copy .env.example and set HORIZONS_BASE_URL + INGEST_API_KEY"
    exit 1
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $name = $line.Substring(0, $i).Trim()
    $value = $line.Substring($i + 1).Trim()
    if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

if (-not $env:HORIZONS_BASE_URL -or -not $env:INGEST_API_KEY) {
    Write-Log "ERROR: HORIZONS_BASE_URL and INGEST_API_KEY must be set in .env"
    exit 1
}

Write-Log ("HORIZONS_BASE_URL={0}" -f $env:HORIZONS_BASE_URL)
Write-Log ("INGEST_API_KEY set (len={0})" -f $env:INGEST_API_KEY.Length)

# Prefer project venv
$py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) {
    Write-Log "ERROR: python not found (create .venv or install Python 3.12+)"
    exit 1
}
Write-Log "python=$py"

$args = @("-m", "ingest.main", "--source", "all")
Write-Log ("running: {0} {1}" -f $py, ($args -join " "))

$exitCode = 0
try {
    & $py @args *>> $LogFile
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }
} catch {
    Write-Log ("ERROR: $($_.Exception.Message)")
    $exitCode = 2
}

Copy-Item $LogFile $LatestLog -Force
Write-Log "=== ingest finished exit=$exitCode log=$LogFile ==="
exit $exitCode
