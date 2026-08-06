# Install free daily Windows Task Scheduler job for disclosure ingest.
# Run once in PowerShell (no admin required for current-user task):
#   powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
#
# Optional: -Hour 5 -Minute 0  (local time; default 5:00 AM local)

param(
    [int]$Hour = 5,
    [int]$Minute = 0,
    [string]$TaskName = "RollcalltrackDisclosureIngest",
    [string]$IngestApiKey = "",
    [switch]$SkipKeyPrompt
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunScript = Join-Path $RepoRoot "scripts\run_daily_ingest.ps1"
$EnvFile = Join-Path $RepoRoot ".env"
$VenvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Req = Join-Path $RepoRoot "requirements.txt"

Write-Host "Repo: $RepoRoot"

# 1) .env
if (-not (Test-Path $EnvFile)) {
    $example = Join-Path $RepoRoot ".env.example"
    Copy-Item $example $EnvFile
    Write-Host "Created .env from .env.example"
}

# Ensure base URL is set
$envText = Get-Content $EnvFile -Raw
if ($envText -notmatch "HORIZONS_BASE_URL=https://") {
    $defaultBase = "https://0e339d55-4da0-4736-afae-facb91d059ee.app-preview.com"
    if ($envText -match "HORIZONS_BASE_URL=.*") {
        $envText = $envText -replace "HORIZONS_BASE_URL=.*", "HORIZONS_BASE_URL=$defaultBase"
    } else {
        $envText += "`nHORIZONS_BASE_URL=$defaultBase`n"
    }
    Set-Content -Path $EnvFile -Value $envText -Encoding utf8
    Write-Host "Set HORIZONS_BASE_URL in .env"
}

function Set-EnvKey([string]$plain) {
    $lines = Get-Content $EnvFile
    $written = $false
    $out = foreach ($ln in $lines) {
        if ($ln -match '^\s*INGEST_API_KEY\s*=') {
            $written = $true
            "INGEST_API_KEY=$plain"
        } else { $ln }
    }
    if (-not $written) { $out += "INGEST_API_KEY=$plain" }
    Set-Content -Path $EnvFile -Value $out -Encoding utf8
}

$keyLine = (Get-Content $EnvFile | Where-Object { $_ -match '^\s*INGEST_API_KEY\s*=' } | Select-Object -First 1)
$keyVal = ""
if ($keyLine) {
    $keyVal = ($keyLine -split "=", 2)[1].Trim().Trim('"').Trim("'")
}
if (-not $IngestApiKey -and $env:INGEST_API_KEY) { $IngestApiKey = $env:INGEST_API_KEY }
if (-not $IngestApiKey -and $env:X_INGEST_KEY) { $IngestApiKey = $env:X_INGEST_KEY }

$placeholder = @("your_ingest_api_key_here", "", "changeme", "YOUR_KEY")
$needsKey = ($placeholder -contains $keyVal -or $keyVal.Length -lt 8)

if ($IngestApiKey -and $IngestApiKey.Length -ge 8) {
    Set-EnvKey $IngestApiKey
    Write-Host "Wrote INGEST_API_KEY to .env"
    $needsKey = $false
}

if ($needsKey -and -not $SkipKeyPrompt) {
    Write-Host ""
    Write-Host "INGEST_API_KEY is missing or still a placeholder."
    Write-Host "Paste the key from Horizons environment variables (INGEST_API_KEY / X-Ingest-Key)."
    Write-Host "It will only be stored in local .env (gitignored)."
    $secure = Read-Host "INGEST_API_KEY" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $plain -or $plain.Length -lt 8) {
        throw "INGEST_API_KEY is required. Re-run with -IngestApiKey '...' or edit .env"
    }
    Set-EnvKey $plain
    Write-Host "Wrote INGEST_API_KEY to .env"
} elseif ($needsKey) {
    Write-Host "WARNING: INGEST_API_KEY not set. Edit .env before the first run."
}

# 2) venv + deps
$pySys = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pySys) { throw "Python not found on PATH. Install Python 3.12+ first." }

if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating .venv ..."
    & $pySys -m venv (Join-Path $RepoRoot ".venv")
}
Write-Host "Installing requirements ..."
& $VenvPy -m pip install --upgrade pip | Out-Host
& $VenvPy -m pip install -r $Req | Out-Host

# 3) Scheduled task (current user, runs whether logged on or not if password stored —
#    default: only when user is logged on, free, no admin)
$ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arg = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`""

$action = New-ScheduledTaskAction -Execute $ps -Argument $arg -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour $Hour -Minute $Minute -Second 0)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "rollcalltrack.com House/Senate disclosure ingest -> Horizons API (free local)" `
    -Force | Out-Null

Write-Host ""
Write-Host "Installed scheduled task: $TaskName"
Write-Host ("Schedule: daily at {0:D2}:{1:D2} local time" -f $Hour, $Minute)
Write-Host "Script:   $RunScript"
Write-Host "Logs:     $(Join-Path $RepoRoot 'logs')"
Write-Host ""
Write-Host "Test now:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$RunScript`""
Write-Host "  # or:  schtasks /Run /TN `"$TaskName`""
Write-Host ""
Write-Host "Uninstall:  Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
