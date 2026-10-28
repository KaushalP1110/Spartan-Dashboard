<#
Spartan Dashboard - pack what's needed to move the dashboard to another PC/VM.

    powershell -ExecutionPolicy Bypass -File .\Spartan-Dashboard\export-bundle.ps1

Creates spartan-move.zip on your Desktop with what is NOT on GitHub:
  secrets\   the .env files of Spartan-Dashboard, Reminder-Sanity-Automation
             and Recall-Status-Automation, and the Google service-account key
  projects\  Recall-Status-Automation (not on GitHub) and CronReporting
             (working copy, so local fixes come along)
  data\      the dashboard's run history (runs.json + logs)

On the new PC, after cloning Spartan-Dashboard:
    powershell -ExecutionPolicy Bypass -File .\Spartan-Dashboard\install.ps1 -Bundle <path>\spartan-move.zip

The zip holds passwords and tokens: copy it straight to the new PC and
delete it from both PCs afterwards. Never commit or share it.
#>
param(
    [string]$Out = (Join-Path ([Environment]::GetFolderPath("Desktop")) "spartan-move.zip")
)

$ErrorActionPreference = "Stop"
$Dashboard = $PSScriptRoot
$Root = Split-Path $Dashboard -Parent

function Ok($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  !!  $text" -ForegroundColor Yellow }
function Get-EnvValue($file, $key) {
    if (-not (Test-Path $file)) { return $null }
    $line = Get-Content $file | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$key=", "").Trim().Trim('"').Trim("'") }
    return $null
}
function Copy-Tree($from, $to, [string[]]$excludeDirs, [string[]]$excludeFiles) {
    $rcArgs = @($from, $to, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($excludeDirs) { $rcArgs += "/XD"; $rcArgs += $excludeDirs }
    if ($excludeFiles) { $rcArgs += "/XF"; $rcArgs += $excludeFiles }
    & robocopy @rcArgs | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $from failed (robocopy exit $LASTEXITCODE)" }
}

Write-Host "Spartan Dashboard - export bundle" -ForegroundColor White
$stage = Join-Path $env:TEMP ("spartan-move-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force "$stage\secrets\keys", "$stage\projects", "$stage\data" | Out-Null
try {
    # --- secrets ---------------------------------------------------------------
    $keys = @{}
    foreach ($name in @("Spartan-Dashboard", "Reminder-Sanity-Automation", "Recall-Status-Automation")) {
        $envFile = Join-Path $Root "$name\.env"
        if (-not (Test-Path $envFile)) { Warn "$name\.env not found - not included"; continue }
        Copy-Item $envFile "$stage\secrets\$name.env"
        Ok "$name\.env"
        $key = Get-EnvValue $envFile "GDRIVE_SERVICE_ACCOUNT_FILE"
        if ($key) { $keys[$key] = $true }
    }
    foreach ($key in $keys.Keys) {
        if (Test-Path $key) { Copy-Item $key "$stage\secrets\keys\"; Ok "Google service-account key ($(Split-Path $key -Leaf))" }
        else { Warn "Service-account key '$key' (from GDRIVE_SERVICE_ACCOUNT_FILE) not found - not included" }
    }

    # --- projects not (fully) on GitHub ------------------------------------------------
    $junkDirs = @("__pycache__", ".venv", "venv", ".idea", ".pytest_cache", "target", "node_modules")
    $recall = Join-Path $Root "Recall-Status-Automation"
    if (Test-Path $recall) {
        Copy-Tree $recall "$stage\projects\Recall-Status-Automation" ($junkDirs + @("$recall\reports\output")) @(".env")
        Ok "Recall-Status-Automation"
    } else { Warn "Recall-Status-Automation not found at $recall - not included" }
    $cron = Join-Path $Root "CronReporting"
    if (Test-Path $cron) {
        Copy-Tree $cron "$stage\projects\CronReporting" ($junkDirs + @("$cron\git-filter-repo-main")) @("*.xlsx", "hs_err*.log", "replay*.log")
        Ok "CronReporting (working copy, with its local changes)"
    } else { Warn "CronReporting not found at $cron - not included" }

    # --- run history --------------------------------------------------------------------
    $data = Join-Path $Dashboard "data"
    if (Test-Path "$data\runs.json") {
        Copy-Item "$data\runs.json" "$stage\data\"
        if (Test-Path "$data\logs") { Copy-Tree "$data\logs" "$stage\data\logs" @() @() }
        Ok "Run history ($((Get-Content "$data\runs.json" -Raw | ConvertFrom-Json).Count) runs)"
    } else { Warn "No run history (data\runs.json) - not included" }

    # --- zip ------------------------------------------------------------------------------
    if (Test-Path $Out) { Remove-Item $Out -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($stage, $Out, [IO.Compression.CompressionLevel]::Optimal, $false)
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Bundle: $Out ($([math]::Round((Get-Item $Out).Length / 1MB, 1)) MB)" -ForegroundColor White
Write-Host "It contains passwords and tokens - copy it straight to the new PC and delete it from both PCs afterwards." -ForegroundColor Yellow
