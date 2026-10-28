<#
Spartan Dashboard - set up (or update) the dashboard on this Windows PC.

  1. Pick the folder where the QA projects should live (e.g. C:\QA) and run:
         git clone https://github.com/KaushalP1110/Spartan-Dashboard.git
  2. Run this script from that clone:
         powershell -ExecutionPolicy Bypass -File .\Spartan-Dashboard\install.ps1
     Check only, change nothing:
         powershell -ExecutionPolicy Bypass -File .\Spartan-Dashboard\install.ps1 -CheckOnly

It clones/updates the automation projects next to Spartan-Dashboard, installs
their Python packages, creates the dashboard's .env (asks for the team
password and Jenkins login), opens the firewall port (asks first, needs
admin), registers the "Spartan Dashboard" logon task, starts the dashboard
and prints a checklist of what still needs a person. Safe to run again.

Secrets are never downloaded: the projects' own .env files and the Google
Drive service-account key must be copied over by hand - the checklist says
which are missing.

Moving from another PC: run export-bundle.ps1 there, copy the zip here and
pass it with -Bundle. It fills in what isn't on GitHub (the .env files, the
service-account key, Recall-Status-Automation, CronReporting's working copy,
the run history) - only where this PC doesn't have it yet:
    powershell -ExecutionPolicy Bypass -File .\Spartan-Dashboard\install.ps1 -Bundle $HOME\Desktop\spartan-move.zip
#>
param(
    [string]$JenkinsUrl = "http://localhost:8080",  # Jenkins runs on this PC? keep localhost
    [string]$OpenDentalDbHost = "",                 # PC with OpenDental's MySQL, e.g. 172.16.3.71
    [int]$Port = 8765,
    [string]$Bundle = "",                           # spartan-move.zip from export-bundle.ps1 (moving from another PC)
    [switch]$NoAutoStart,                           # set up, but don't register or start the logon task
    [switch]$CheckOnly                              # report only, change nothing
)

$ErrorActionPreference = "Stop"
$Dashboard = $PSScriptRoot
$Root = Split-Path $Dashboard -Parent
$TaskName = "Spartan Dashboard"
$script:Todo = New-Object System.Collections.Generic.List[string]
$script:BundleKeys = @{}    # key file name -> where -Bundle put it
$script:PendingEnv = $null  # Reminder Sanity .env from -Bundle, placed once step 2 has cloned it

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Info($text) { Write-Host "      $text" }
function Warn($text) { Write-Host "  !!  $text" -ForegroundColor Yellow; $script:Todo.Add($text) }
function Ask($question) {
    if ($CheckOnly) { return $false }
    return (Read-Host "  ?   $question [y/N]") -match '^(y|yes)$'
}
function Read-Secret($prompt) {
    $secure = Read-Host "  ?   $prompt" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
function Get-EnvValue($file, $key) {
    if (-not (Test-Path $file)) { return $null }
    $line = Get-Content $file | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$key=", "").Trim().Trim('"').Trim("'") }
    return $null
}
function Set-EnvValue($file, $key, $value) {
    $lines = @(Get-Content $file)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$key=") { $lines[$i] = "$key=$value"; $found = $true }
    }
    if (-not $found) { $lines += "$key=$value" }
    [IO.File]::WriteAllLines($file, $lines)  # UTF-8 without BOM
}
function Test-Port($hostName, $portNumber) {
    $client = New-Object Net.Sockets.TcpClient
    try { return $client.ConnectAsync($hostName, $portNumber).Wait(3000) -and $client.Connected } catch { return $false } finally { $client.Dispose() }
}

Write-Host "Spartan Dashboard setup" -ForegroundColor White
Info "Dashboard: $Dashboard"
Info "Projects : $Root"
if ($CheckOnly) { Info "Mode     : check only - nothing will be changed" }

# --- 1. Tools ----------------------------------------------------------------
Step "1. Tools"
$git = Get-Command git -ErrorAction SilentlyContinue
if ($git) { Ok "git" } else { Warn "git is not installed - install Git for Windows, then run this again"; return }

$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    $exe = Get-Command $candidate[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    try {
        $out = & $candidate[0] @($candidate[1..9] | Where-Object { $_ }) -c "import sys; print(sys.executable); print(sys.version_info >= (3, 10))" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and $out[1] -eq "True") { $python = $out[0]; break }
    } catch { }
}
if ($python) { Ok "Python 3.10+ ($python)" } else { Warn "Python 3.10 or newer not found - install it from python.org (tick 'Add to PATH'), then run this again"; return }

$mvn = Get-Command mvn -ErrorAction SilentlyContinue
$ideaMvn = Get-ChildItem "C:\Program Files\JetBrains\*\plugins\maven\lib\maven3\bin\mvn.cmd" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($mvn) { Ok "Maven ($($mvn.Source))" }
elseif ($ideaMvn) { Ok "Maven (IntelliJ's: $($ideaMvn.FullName))" }
else { Warn "Maven not found (needed for Cron Reporting) - install Maven or IntelliJ, or set MAVEN_CMD in Spartan-Dashboard\.env" }

$jdk = @(Get-ChildItem "$env:USERPROFILE\.jdks\*21*", "C:\Program Files\Java\jdk-21*", "C:\Program Files\Eclipse Adoptium\jdk-21*" -Directory -ErrorAction SilentlyContinue) + @(Get-ChildItem "C:\Program Files\JetBrains\*\jbr" -Directory -ErrorAction SilentlyContinue)
if ($env:JAVA_HOME -or $jdk) { Ok "Java (JAVA_HOME or a JDK 21 was found)" } else { Warn "JDK 21 not found (needed for the Maven projects) - install it, or set JAVA_HOME in Spartan-Dashboard\.env" }

$browser = @("C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "C:\Program Files\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($browser) { Ok "Edge/Chrome for PDF reports" } else { Warn "Edge/Chrome not found - Reminder Sanity / Recall Status PDF reports will be skipped" }

# --- 1b. Bundle from the old PC (optional) ------------------------------------------
if ($Bundle) {
    Step "1b. Bundle from the old PC ($Bundle)"
    if (-not (Test-Path $Bundle)) { Warn "Bundle '$Bundle' not found - copy spartan-move.zip here first"; return }
    $unpacked = Join-Path $env:TEMP ("spartan-bundle-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory((Resolve-Path $Bundle).Path, $unpacked)
    try {
        # Projects that aren't (fully) on GitHub - only where missing, so step 2 won't clone over them.
        foreach ($dir in @(Get-ChildItem "$unpacked\projects" -Directory -ErrorAction SilentlyContinue)) {
            $target = Join-Path $Root $dir.Name
            if (Test-Path $target) { Info "$($dir.Name) already here - kept as is" }
            elseif ($CheckOnly) { Info "$($dir.Name) would be copied from the bundle" }
            else { Copy-Item $dir.FullName $target -Recurse; Ok "$($dir.Name) copied from the bundle" }
        }

        # Service-account key -> <projects>\secrets, then point every .env at it.
        $keyDir = Join-Path $Root "secrets"
        $keyMap = @{}
        foreach ($key in @(Get-ChildItem "$unpacked\secrets\keys" -File -ErrorAction SilentlyContinue)) {
            $keyMap[$key.Name] = Join-Path $keyDir $key.Name
            if ($CheckOnly) { Info "Key $($key.Name) would be placed in $keyDir" }
            elseif (-not (Test-Path $keyMap[$key.Name])) {
                New-Item -ItemType Directory -Force $keyDir | Out-Null
                Copy-Item $key.FullName $keyMap[$key.Name]
                Ok "Google service-account key -> $($keyMap[$key.Name])"
            }
        }

        foreach ($envFile in @(Get-ChildItem "$unpacked\secrets" -Filter "*.env" -File -ErrorAction SilentlyContinue)) {
            $name = $envFile.BaseName
            $projectDir = Join-Path $Root $name
            $target = Join-Path $projectDir ".env"
            if ($CheckOnly) { Info "$name\.env would be $(if (Test-Path $target) { 'kept (exists)' } else { 'copied from the bundle' })"; continue }
            if ($name -eq "Reminder-Sanity-Automation" -and -not (Test-Path $projectDir)) {
                # Cloned in step 2 - park the .env until then.
                $script:PendingEnv = @{ From = $envFile.FullName; To = $target }
                continue
            }
            if (-not (Test-Path $projectDir)) { Warn "$name is not here - its .env from the bundle was not used"; continue }
            if (Test-Path $target) { Info "$name\.env exists - kept as is" }
            else { Copy-Item $envFile.FullName $target; Ok "$name\.env copied from the bundle" }
        }

        # Run history - only if this dashboard has none yet.
        $dataDir = Join-Path $Dashboard "data"
        if (Test-Path "$unpacked\data\runs.json") {
            if (Test-Path "$dataDir\runs.json") { Info "Run history exists here - kept as is" }
            elseif ($CheckOnly) { Info "Run history would be copied from the bundle" }
            else {
                New-Item -ItemType Directory -Force $dataDir | Out-Null
                Copy-Item "$unpacked\data\runs.json" $dataDir
                if (Test-Path "$unpacked\data\logs") { Copy-Item "$unpacked\data\logs" $dataDir -Recurse -Force }
                Ok "Run history copied from the bundle"
            }
        }
        $script:BundleKeys = $keyMap
    } finally {
        if ($script:PendingEnv) {
            $parked = Join-Path $env:TEMP "spartan-bundle-reminder-sanity.env"
            Copy-Item $script:PendingEnv.From $parked -Force
            $script:PendingEnv.From = $parked
        }
        Remove-Item $unpacked -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- 2. Projects ---------------------------------------------------------------
Step "2. Projects (next to Spartan-Dashboard)"
if (Test-Path (Join-Path $Dashboard ".git")) {
    $dirty = git -C $Dashboard status --porcelain --untracked-files=no
    if ($CheckOnly -or $dirty) { Info "Spartan-Dashboard: not updated$(if ($dirty) { ' (has local changes)' })" }
    else { git -C $Dashboard pull --ff-only -q; Ok "Spartan-Dashboard updated" }
}
$repos = @(
    @{ Name = "Reminder-Sanity-Automation"; Url = "https://github.com/KaushalP1110/Reminder-Sanity-Automation.git"; Branch = "main" },
    @{ Name = "CronReporting"; Url = "https://github.com/ShivamBhayani1/CronReporting.git"; Branch = "Development" }
)
foreach ($repo in $repos) {
    $dir = Join-Path $Root $repo.Name
    if (-not (Test-Path $dir)) {
        if ($CheckOnly) { Warn "$($repo.Name) is missing (would be cloned from $($repo.Url))"; continue }
        git clone -q -b $repo.Branch $repo.Url $dir
        if ($LASTEXITCODE -eq 0) { Ok "$($repo.Name) cloned" } else { Warn "Could not clone $($repo.Name) - check your GitHub access to $($repo.Url)" }
    } elseif (Test-Path (Join-Path $dir ".git")) {
        $dirty = git -C $dir status --porcelain --untracked-files=no
        if ($dirty) { Info "$($repo.Name): has local changes - not updated" }
        elseif ($CheckOnly) { Ok "$($repo.Name) present" }
        else { git -C $dir pull --ff-only -q; if ($LASTEXITCODE -eq 0) { Ok "$($repo.Name) updated" } else { Warn "$($repo.Name): git pull failed - update it by hand" } }
    } else { Ok "$($repo.Name) present (not a git folder)" }
}
$recall = Join-Path $Root "Recall-Status-Automation"
if (Test-Path $recall) { Ok "Recall-Status-Automation present" }
else { Warn "Recall-Status-Automation is not on GitHub - copy the whole folder from Kaushal's PC (IdeaProjects\Recall-Status-Automation) to $recall" }

# Bundle, part 2 (now that the projects are cloned): the parked .env, and
# paths in the .env files that pointed at the old PC.
if ($Bundle -and -not $CheckOnly) {
    if ($script:PendingEnv) {
        $to = $script:PendingEnv.To
        if (-not (Test-Path (Split-Path $to -Parent))) { Warn "Reminder-Sanity-Automation was not cloned - its .env from the bundle was not used" }
        elseif (Test-Path $to) { Info "Reminder-Sanity-Automation\.env exists - kept as is" }
        else { Copy-Item $script:PendingEnv.From $to; Ok "Reminder-Sanity-Automation\.env copied from the bundle" }
        Remove-Item $script:PendingEnv.From -Force -ErrorAction SilentlyContinue
    }
    foreach ($name in @("Spartan-Dashboard", "Reminder-Sanity-Automation", "Recall-Status-Automation")) {
        $envFile = Join-Path $Root "$name\.env"
        $key = Get-EnvValue $envFile "GDRIVE_SERVICE_ACCOUNT_FILE"
        if (-not $key -or (Test-Path $key)) { continue }
        $newKey = $script:BundleKeys[(Split-Path $key -Leaf)]
        if ($newKey -and (Test-Path $newKey)) { Set-EnvValue $envFile "GDRIVE_SERVICE_ACCOUNT_FILE" $newKey; Ok "$name\.env: GDRIVE_SERVICE_ACCOUNT_FILE -> $newKey" }
    }
    $dashEnvFile = Join-Path $Dashboard ".env"
    foreach ($key in @("MAVEN_CMD", "JAVA_HOME")) {
        $value = Get-EnvValue $dashEnvFile $key
        if ($value -and -not (Test-Path $value)) { Set-EnvValue $dashEnvFile $key ""; Ok "Spartan-Dashboard\.env: $key cleared (old PC's path; detected automatically)" }
    }
}

# --- 3. Python packages -----------------------------------------------------------
Step "3. Python packages"
foreach ($name in @("Reminder-Sanity-Automation", "Recall-Status-Automation")) {
    $req = Join-Path $Root "$name\requirements.txt"
    if (-not (Test-Path $req)) { continue }
    if ($CheckOnly) { Info "$name - would run: pip install -r requirements.txt"; continue }
    & $python -m pip install -q --disable-pip-version-check -r $req
    if ($LASTEXITCODE -eq 0) { Ok "$name packages" } else { Warn "pip install failed for $name - run: `"$python`" -m pip install -r `"$req`"" }
}

# --- 4. Projects' settings (secrets are copied by hand) -----------------------------
Step "4. Projects' settings"
foreach ($name in @("Reminder-Sanity-Automation", "Recall-Status-Automation")) {
    $envFile = Join-Path $Root "$name\.env"
    if (-not (Test-Path (Join-Path $Root $name))) { continue }
    if (-not (Test-Path $envFile)) { Warn "$name\.env is missing - copy it from Kaushal's PC (it holds the Adit login and Google settings)"; continue }
    Ok "$name\.env"
    $keyFile = Get-EnvValue $envFile "GDRIVE_SERVICE_ACCOUNT_FILE"
    if ($keyFile -and -not (Test-Path $keyFile)) { Warn "$name\.env points GDRIVE_SERVICE_ACCOUNT_FILE at '$keyFile', which is not on this PC - copy the Google service-account JSON there (or fix the path)" }
}

# OpenDental's MySQL: the projects' configs say 127.0.0.1 (written for the PC
# running OpenDental). On any other PC they must point at that PC.
$rsConfig = Join-Path $Root "Reminder-Sanity-Automation\vendor\phase1_opendental\config\config.yaml"
$recallEnv = Join-Path $recall ".env"
$currentDb = if (Test-Path $recallEnv) { Get-EnvValue $recallEnv "OD_DB_HOST" } else { $null }
if (-not $currentDb -and (Test-Path $rsConfig)) {
    $m = Select-String -Path $rsConfig -Pattern '^\s+host:\s*"?([^"\s]+)"?' | Select-Object -First 1
    if ($m) { $currentDb = $m.Matches[0].Groups[1].Value }
}
if (-not $OpenDentalDbHost -and -not $CheckOnly) {
    $answer = Read-Host "  ?   OpenDental MySQL host (the PC running OpenDental) [$currentDb]"
    $OpenDentalDbHost = if ($answer) { $answer.Trim() } else { $currentDb }
}
if (-not $OpenDentalDbHost) { $OpenDentalDbHost = $currentDb }
if ($OpenDentalDbHost) {
    if ($OpenDentalDbHost -ne $currentDb -and -not $CheckOnly) {
        if (Test-Path $recallEnv) { Set-EnvValue $recallEnv "OD_DB_HOST" $OpenDentalDbHost; Ok "Recall-Status-Automation\.env: OD_DB_HOST=$OpenDentalDbHost" }
        if (Test-Path $rsConfig) {
            $text = [IO.File]::ReadAllText($rsConfig)
            $text = [regex]::Replace($text, '(?m)^(\s+host:\s*)"?[^"\r\n#]+"?', "`${1}`"$OpenDentalDbHost`"", 1)
            [IO.File]::WriteAllText($rsConfig, $text)
            Ok "Reminder Sanity config.yaml: host $OpenDentalDbHost"
        }
    }
    if (Test-Port $OpenDentalDbHost 3306) { Ok "OpenDental MySQL reachable at ${OpenDentalDbHost}:3306" }
    else { Warn "OpenDental MySQL at ${OpenDentalDbHost}:3306 is not reachable - that PC must be on, and its MySQL must allow remote connections from this PC (bind-address, user grant, firewall port 3306). Reminder Sanity and Recall Status need it." }
}

# --- 5. Dashboard settings ------------------------------------------------------------
Step "5. Dashboard settings (Spartan-Dashboard\.env)"
$dashEnv = Join-Path $Dashboard ".env"
if (-not (Test-Path $dashEnv)) {
    if ($CheckOnly) { Warn "Spartan-Dashboard\.env is missing (the script creates it when run without -CheckOnly)" }
    else {
        Copy-Item (Join-Path $Dashboard ".env.example") $dashEnv
        $password = Read-Secret "Team password for the dashboard (people sign in with it)"
        Set-EnvValue $dashEnv "DASHBOARD_PASSWORD" $password
        Set-EnvValue $dashEnv "DASHBOARD_HOST" "0.0.0.0"
        Set-EnvValue $dashEnv "DASHBOARD_PORT" "$Port"
        $answer = Read-Host "  ?   Jenkins address [$JenkinsUrl]"
        Set-EnvValue $dashEnv "JENKINS_URL" $(if ($answer) { $answer.Trim() } else { $JenkinsUrl })
        Set-EnvValue $dashEnv "JENKINS_USER" (Read-Host "  ?   Jenkins username (for running Reminder UI)")
        Set-EnvValue $dashEnv "JENKINS_API_TOKEN" (Read-Secret "Jenkins API token (Jenkins > your name > Security > API Token)")
        Ok "Spartan-Dashboard\.env created"
    }
} else { Ok "Spartan-Dashboard\.env exists (kept as is)" }
if (Test-Path $dashEnv) {
    foreach ($key in @("DASHBOARD_PASSWORD", "JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN")) {
        if (-not (Get-EnvValue $dashEnv $key)) { Warn "Spartan-Dashboard\.env: $key is empty" }
    }
    $docsKey = Get-EnvValue $dashEnv "GDRIVE_SERVICE_ACCOUNT_FILE"
    if (-not $docsKey) { Warn "Spartan-Dashboard\.env: GDRIVE_SERVICE_ACCOUNT_FILE is empty - the Docs & Test Cases tab needs the Google service-account key" }
    elseif (-not (Test-Path $docsKey)) { Warn "Spartan-Dashboard\.env: GDRIVE_SERVICE_ACCOUNT_FILE points at '$docsKey', which is not on this PC - copy the key there (or fix the path)" }
    else { Ok "Google service-account key for Docs & Test Cases" }
    $jenkins = Get-EnvValue $dashEnv "JENKINS_URL"
    if ($jenkins) {
        try { $u = [Uri]$jenkins; if (Test-Port $u.Host $u.Port) { Ok "Jenkins reachable at $jenkins" } else { Warn "Jenkins at $jenkins is not reachable - Reminder UI can't run until it is" } } catch { Warn "JENKINS_URL '$jenkins' is not a valid address" }
    }
}

# --- 6. Firewall ------------------------------------------------------------------------
Step "6. Firewall (so the team can open the dashboard)"
$netCategory = "$((Get-NetConnectionProfile | Select-Object -First 1).NetworkCategory)"
$fwProfile = if ($netCategory -like "Domain*") { "Domain" } elseif ($netCategory -eq "Private") { "Private" } else { "Public" }
$fwOn = (Get-NetFirewallProfile -Name $fwProfile -ErrorAction SilentlyContinue).Enabled
$rule = Get-NetFirewallRule -DisplayName "Spartan Dashboard" -ErrorAction SilentlyContinue
if (-not $fwOn) { Ok "Firewall is off for the current network ($fwProfile)" }
elseif ($rule) { Ok "Firewall rule 'Spartan Dashboard' exists" }
else {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin -and (Ask "Allow incoming connections on port $Port so teammates can open the dashboard?")) {
        New-NetFirewallRule -DisplayName "Spartan Dashboard" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain, Private, Public | Out-Null
        Ok "Firewall rule added for port $Port"
    } else {
        Warn "Firewall may block teammates - in an admin PowerShell run: New-NetFirewallRule -DisplayName 'Spartan Dashboard' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow"
    }
}

# --- 7. Auto-start + start ------------------------------------------------------------------
Step "7. Auto-start at logon, and start it now"
if ($CheckOnly) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { Ok "Task '$TaskName' exists" } else { Info "Task '$TaskName' would be created" }
} elseif ($NoAutoStart) {
    Info "Skipped (-NoAutoStart) - start it by hand: python -u server.py (in $Dashboard)"
} else {
    New-Item -ItemType Directory -Force (Join-Path $Dashboard "data") | Out-Null
    $launch = "Start-Process -FilePath '$python' -ArgumentList '-u','server.py' -WorkingDirectory '$Dashboard' -WindowStyle Hidden " +
              "-RedirectStandardOutput '$Dashboard\data\server.log' -RedirectStandardError '$Dashboard\data\server.err.log'"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$launch`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $trigger.Delay = "PT30S"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Description "Starts the Spartan Dashboard at logon. Log: $Dashboard\data\server.log" `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Ok "Task '$TaskName' registered (starts 30s after you log in)"

    # One copy only: stop a dashboard already listening on the port, then start via the task.
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName
    $up = $false
    for ($i = 0; $i -lt 15 -and -not $up; $i++) {
        Start-Sleep -Seconds 1
        try { $up = (Invoke-WebRequest "http://127.0.0.1:$Port/login" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { }
    }
    if ($up) { Ok "Dashboard is running" } else { Warn "Dashboard did not start - see $Dashboard\data\server.err.log" }
}
if (Test-Path (Join-Path $Dashboard "data\server.log")) {
    Get-Content (Join-Path $Dashboard "data\server.log") -TotalCount 10 | Where-Object { $_ -match '^\s+(OK|!!)' } | ForEach-Object { Info $_.Trim() }
}

# --- Done --------------------------------------------------------------------------------------
$ips = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } | Select-Object -ExpandProperty IPAddress)
Step "Done"
foreach ($ip in $ips) { Info "Team address:  http://${ip}:$Port" }
$script:Todo.Add("Reminder UI GChat 'Open PDF report' button: in Reminder-UI-Automation-PW\Jenkinsfile set SPARTAN_REPORT_URL = 'http://<this PC's office IP>:$Port/jenkins/reminder_ui/' and push")
$script:Todo.Add("Tell the team the new address, and stop the old dashboard on Kaushal's PC (Unregister-ScheduledTask -TaskName '$TaskName')")
Write-Host ""
Write-Host "Still to do:" -ForegroundColor Yellow
$n = 1
foreach ($item in $script:Todo) { Write-Host "  $n. $item"; $n++ }
