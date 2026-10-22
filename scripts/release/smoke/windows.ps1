<#
.SYNOPSIS
Post-release smoke test for Windows (P25-23).

.DESCRIPTION
    powershell -ExecutionPolicy Bypass -File windows.ps1 -Tag vX.Y.Z [-Repository OWNER/NAME]
        [-Channel stable|canary] [-WorkDir DIR] [-Desktop auto|skip|FILE] [-Interactive] [-Keep]
        [-DryRun]

Run it on a clean Windows machine or VM right after a release, and on the canary rehearsal
artefacts. It uses only what the release published: install.ps1 from the release's downloads, the
desktop installer from the same release, and the release's own checkout that install.ps1 puts on
disk (its scripted model server is the fake-model endpoint, so no real model or key is needed).
The Indus home and the code go into one throwaway folder. Windows has no per-run HOME, so the
install adds its bin folder to this user's PATH and the desktop installer installs per user; the
uninstall steps take both away again.

Each step prints one line, "<step> PASS|FAIL|SKIP <detail>", and the run ends with a summary.
Exit 0 when no step failed, 1 otherwise, 2 for a usage error. -DryRun prints the plan and the URLs
and fetches nothing. Results go into docs/internals/rehearsal-log.md.
#>
[CmdletBinding()]
param(
    [string]$Tag = "",
    [string]$Repository = "",
    [string]$Channel = "",
    [string]$WorkDir = "",
    [string]$Desktop = "auto",
    [switch]$Interactive,
    [switch]$Keep,
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'

$SmokeOs = 'windows'
# The release base (RELEASE_REPOSITORY in src/indus/foundation/versions.py, D-16.690); a unit test
# holds this default equal to it.
$SmokeDefaultRepository = 'indusagi/indus-agent'
$SmokeSteps = @(
    'install', 'version', 'setup', 'turn', 'approval', 'resume', 'doctor', 'dashboard-gate',
    'dashboard-login', 'desktop-download', 'licences', 'signature', 'desktop-launch', 'uninstall'
)
$SmokeInstallWait = 1800
$SmokeTurnWait = 180
$SmokeDashboardWait = 600
$SmokeDesktopWait = 20

function Get-SmokeDescription([string]$Step) {
    switch ($Step) {
        'install' { return "download install.ps1 from the release and install the $script:Channel channel" }
        'version' { return 'indus --version; indus update --check reports this release' }
        'setup' { return 'indus setup --non-interactive; point the model at the fake endpoint' }
        'turn' { return 'one turn through the gateway daemon (indus -z)' }
        'approval' { return 'a risky command without a person to approve it does not run' }
        'resume' { return 'resume the first session by id (indus -r ID -z)' }
        'doctor' { return 'indus doctor passes' }
        'dashboard-gate' { return 'dashboard on loopback: status public, settings refuse a request without the token' }
        'dashboard-login' { return "dashboard page carries the loopback session token and the API accepts it" }
        'desktop-download' { return 'download the desktop installer from the release and install it for this user' }
        'signature' { return 'the installer and the app are signed (Authenticode)' }
        'licences' { return 'THIRD_PARTY_NOTICES.md in the checkout and inside the installed app' }
        'desktop-launch' { return 'start the desktop app and reach the chat (a person confirms)' }
        'uninstall' { return 'indus uninstall -y removes code, launcher and PATH entry and keeps the data' }
        default { return '?' }
    }
}

# -- arguments ------------------------------------------------------------------------------------

if ($Repository -eq '') { $Repository = $SmokeDefaultRepository }
if ($Tag -notmatch '^v[0-9]{1,3}\.[0-9]+\.[0-9]+(-canary\.[0-9]{8}T[0-9]{6}Z)?$') {
    [Console]::Error.WriteLine('a release tag is required: -Tag vX.Y.Z or vX.Y.Z-canary.<YYYYMMDDTHHMMSSZ>')
    exit 2
}
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$') {
    [Console]::Error.WriteLine('-Repository must be OWNER/NAME')
    exit 2
}
$Version = $Tag.Substring(1)
if ($Channel -eq '') {
    if ($Version -like '*-canary.*') { $Channel = 'canary' } else { $Channel = 'stable' }
}
if ($Channel -ne 'stable' -and $Channel -ne 'canary') {
    [Console]::Error.WriteLine('-Channel must be stable or canary')
    exit 2
}
$ReleaseUrl = "https://github.com/$Repository/releases/download/$Tag"

# The installer names electron-builder writes (nsis artifactName Indus-Setup-<version>-<arch>.exe).
function Get-SmokeSetupNames {
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { return @("Indus-Setup-$Version-arm64.exe") }
    return @("Indus-Setup-$Version-x64.exe")
}

function Show-SmokePlan {
    Write-Host "Indus Agent post-release smoke ($SmokeOs)"
    Write-Host "  release:    $Tag ($Channel channel) of $Repository"
    Write-Host "  installer:  $ReleaseUrl/install.ps1"
    if ($Desktop -eq 'auto') {
        foreach ($name in Get-SmokeSetupNames) { Write-Host "  desktop:    $ReleaseUrl/$name" }
    } else {
        Write-Host "  desktop:    $Desktop"
    }
    Write-Host '  fake model: scripts\parity\scripted_model_server.py from the installed checkout'
    $number = 0
    foreach ($step in $SmokeSteps) {
        $number += 1
        Write-Host ('  {0,2}. {1,-17} {2}' -f $number, $step, (Get-SmokeDescription $step))
    }
}

if ($DryRun) {
    Show-SmokePlan
    Write-Host 'Dry run: nothing was fetched or installed.'
    exit 0
}

# -- results and children -------------------------------------------------------------------------

$script:Failed = 0
$script:Results = New-Object System.Collections.Generic.List[string]

function Add-SmokeResult([string]$Step, [string]$Status, [string]$Detail) {
    $line = '{0,-17} {1} {2}' -f $Step, $Status, $Detail
    Write-Host $line
    $script:Results.Add($line)
    if ($Status -eq 'FAIL') { $script:Failed += 1 }
}

function Stop-SmokeTree([int]$ProcessId) {
    if ($ProcessId -le 0) { return }
    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue)
    foreach ($child in $children) { Stop-SmokeTree ([int]$child.ProcessId) }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function ConvertTo-SmokeArgument([string]$Value) {
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

# Invoke-Bounded SECONDS LOG FILE ARGS...: run FILE with ARGS, stdin empty, stdout and stderr into
# LOG; stop it (and what it started) by its own pid after SECONDS. Returns the exit code, or 124.
function Invoke-Bounded([int]$Seconds, [string]$Log, [string]$FilePath, [string[]]$Arguments = @(), [string]$WorkingDirectory = '') {
    if ($WorkingDirectory -eq '') { $WorkingDirectory = $script:Work }
    $joined = ($Arguments | ForEach-Object { ConvertTo-SmokeArgument $_ }) -join ' '
    $start = @{
        FilePath               = $FilePath
        WorkingDirectory       = $WorkingDirectory
        RedirectStandardInput  = $script:Empty
        RedirectStandardOutput = "$Log.out"
        RedirectStandardError  = "$Log.err"
        NoNewWindow            = $true
        PassThru               = $true
    }
    if ($joined -ne '') { $start.ArgumentList = $joined }
    try {
        $process = Start-Process @start
    } catch {
        Set-Content -Path $Log -Value "could not start ${FilePath}: $($_.Exception.Message)"
        return 127
    }
    $null = $process.Handle  # keeps the exit code readable after the process ends
    if (-not $process.WaitForExit($Seconds * 1000)) {
        Stop-SmokeTree $process.Id
        $code = 124
    } else {
        $process.WaitForExit()
        $code = $process.ExitCode
    }
    $text = @()
    foreach ($part in @("$Log.out", "$Log.err")) {
        if (Test-Path $part) { $text += Get-Content -Path $part -ErrorAction SilentlyContinue; Remove-Item $part -Force }
    }
    Set-Content -Path $Log -Value $text
    return $code
}

function Get-LastLine([string]$Path) {
    if (-not (Test-Path $Path)) { return '' }
    $lines = @(Get-Content -Path $Path | Where-Object { $_.Trim() -ne '' })
    if ($lines.Count -eq 0) { return '' }
    $last = [string]$lines[$lines.Count - 1]
    if ($last.Length -gt 160) { $last = $last.Substring(0, 160) }
    return $last
}

function Test-LogContains([string]$Path, [string]$Text) {
    if (-not (Test-Path $Path)) { return $false }
    return [bool](Select-String -Path $Path -SimpleMatch -Pattern $Text -Quiet)
}

function Invoke-Indus([int]$Seconds, [string]$Log, [string[]]$Arguments, [string]$WorkingDirectory = '') {
    return Invoke-Bounded $Seconds $Log $script:Indus $Arguments $WorkingDirectory
}

function Get-SmokeHttp([string]$Url, [hashtable]$Headers = @{}) {
    try {
        $response = Invoke-WebRequest -Uri $Url -Headers $Headers -UseBasicParsing -TimeoutSec 20
        $script:HttpBody = [string]$response.Content
        return [int]$response.StatusCode
    } catch {
        $script:HttpBody = ''
        $failed = $_.Exception.Response
        if ($null -ne $failed) { return [int]$failed.StatusCode }
        return 0
    }
}

# -- the steps ------------------------------------------------------------------------------------

function Step-Install {
    $script:InstallerFile = Join-Path $script:Work 'install.ps1'
    try {
        Invoke-WebRequest -Uri "$ReleaseUrl/install.ps1" -OutFile $script:InstallerFile -UseBasicParsing -TimeoutSec 300
    } catch {
        Add-SmokeResult 'install' 'FAIL' "could not download $ReleaseUrl/install.ps1 (is the installer attached to the release?)"
        return $false
    }
    $shell = (Get-Process -Id $PID).Path
    $log = Join-Path $script:Logs 'install.log'
    $code = Invoke-Bounded $SmokeInstallWait $log $shell @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:InstallerFile,
        '-Channel', $Channel, '-IndusHome', $script:IndusHome, '-InstallDir', $script:Checkout,
        '-NonInteractive', '-SkipBrowser'
    )
    if ($code -ne 0) {
        Add-SmokeResult 'install' 'FAIL' "install.ps1 failed ($code): $(Get-LastLine $log)"
        return $false
    }
    if (-not (Test-Path $script:Indus)) {
        Add-SmokeResult 'install' 'FAIL' "no launcher at $($script:Indus) after the install"
        return $false
    }
    $git = Get-SmokeGit
    $installed = ''
    if ($git) { $installed = (& $git -C $script:Checkout describe --tags --exact-match HEAD 2>$null) }
    if ([string]$installed -ne $Tag) {
        Add-SmokeResult 'install' 'FAIL' "installed $installed, expected $Tag (a newer $Channel release?)"
        return $false
    }
    Add-SmokeResult 'install' 'PASS' "$Tag in $($script:Checkout)"
    return $true
}

function Get-SmokeGit {
    $found = Get-Command git -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    $portable = Get-ChildItem -Path (Join-Path $script:IndusHome 'tools') -Filter git.exe -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.DirectoryName -like '*\cmd' } | Select-Object -First 1
    if ($portable) { return $portable.FullName }
    return $null
}

function Step-Version {
    $log = Join-Path $script:Logs 'version.log'
    $code = Invoke-Indus 60 $log @('--version')
    if ($code -ne 0 -or -not (Test-LogContains $log 'Indus Agent ') -or -not (Test-LogContains $log 'Install method: git')) {
        Add-SmokeResult 'version' 'FAIL' "indus --version: $(Get-LastLine $log)"
        return
    }
    $check = Join-Path $script:Logs 'update-check.log'
    $null = Invoke-Indus 120 $check @('update', '--check')
    $line = Get-LastLine $check
    if ($line -like "*already on $Version*") {
        Add-SmokeResult 'version' 'PASS' "$((Get-Content $log | Select-Object -First 1)); $line"
    } else {
        Add-SmokeResult 'version' 'FAIL' "indus update --check: $line"
    }
}

$script:Model = $null

function Step-Setup {
    $log = Join-Path $script:Logs 'setup.log'
    if ((Invoke-Indus 120 $log @('setup', '--non-interactive')) -ne 0) {
        Add-SmokeResult 'setup' 'FAIL' "indus setup --non-interactive: $(Get-LastLine $log)"
        return
    }
    $python = Join-Path $script:Checkout '.venv\Scripts\python.exe'
    $server = Join-Path $script:Checkout 'scripts\parity\scripted_model_server.py'
    if (-not (Test-Path $server)) {
        Add-SmokeResult 'setup' 'FAIL' "the release checkout has no $server"
        return
    }
    # The server serves until its stdin closes: this script holds the pipe open.
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = (@($server, '--port', '0', '--default-script', 'echo') | ForEach-Object { ConvertTo-SmokeArgument $_ }) -join ' '
    $info.UseShellExecute = $false
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.CreateNoWindow = $true
    try {
        $script:Model = [System.Diagnostics.Process]::Start($info)
    } catch {
        Add-SmokeResult 'setup' 'FAIL' "the fake model did not start: $($_.Exception.Message)"
        return
    }
    $first = $script:Model.StandardOutput.ReadLineAsync()
    $port = ''
    if ($first.Wait(10000) -and ([string]$first.Result -match '"port":\s*([0-9]+)')) { $port = $Matches[1] }
    if ($port -eq '') {
        Add-SmokeResult 'setup' 'FAIL' 'the fake model did not report its port'
        return
    }
    $settings = [ordered]@{
        'model.provider'        = 'custom'
        'model.base_url'        = "http://127.0.0.1:$port/v1"
        'model.default'         = 'echo'
        # Secrets stay in the throwaway home, never in this user's Credential Manager.
        'security.secret_store' = 'file'
    }
    foreach ($key in $settings.Keys) {
        $one = Join-Path $script:Logs "setup-$key.log"
        if ((Invoke-Indus 60 $one @('config', 'set', $key, $settings[$key])) -ne 0) {
            Add-SmokeResult 'setup' 'FAIL' "indus config set ${key}: $(Get-LastLine $one)"
            return
        }
    }
    Add-SmokeResult 'setup' 'PASS' "fake model on 127.0.0.1:$port"
}

function Step-Turn {
    $log = Join-Path $script:Logs 'turn.log'
    $null = Invoke-Indus $SmokeTurnWait $log @('-z', 'remember the blue kite') $script:Project
    if (Test-LogContains $log 'You said: remember the blue kite') {
        Add-SmokeResult 'turn' 'PASS' 'the reply came back through the gateway daemon'
    } else {
        Add-SmokeResult 'turn' 'FAIL' (Get-LastLine $log)
    }
}

function Step-Approval {
    $log = Join-Path $script:Logs 'approval.log'
    $git = Get-SmokeGit
    if (-not $git) {
        Add-SmokeResult 'approval' 'FAIL' 'git was not found (on PATH or under the Indus tools folder)'
        return
    }
    $repo = Join-Path $script:Project 'repo'
    New-Item -ItemType Directory -Force -Path $repo | Out-Null
    $note = Join-Path $repo 'note.txt'
    Set-Content -Path $note -Value 'kept'
    & $git -C $repo init -q *> $null
    & $git -C $repo add note.txt *> $null
    & $git -C $repo -c user.name=smoke -c user.email=smoke@example.invalid commit -qm seed *> $null
    Add-Content -Path $note -Value 'local change'
    # The scripted model asks to run `git reset --hard HEAD` (flagged as destructive). A finite
    # `chat -q` without a terminal has nobody to ask: the run refuses it (single_query_mode) or,
    # through the gateway daemon, leaves the approval waiting for a terminal. Either way nothing
    # may run before a person says yes.
    $null = Invoke-Indus $SmokeTurnWait $log @('-m', 'approve_or_decline', 'chat', '-q', 'please tidy the tree') $repo
    if (-not (Test-LogContains $note 'local change')) {
        Add-SmokeResult 'approval' 'FAIL' 'the risky command ran without an approval'
        return
    }
    if (Test-LogContains $log 'declined') {
        $how = 'refused in an unattended run'
    } elseif (Test-LogContains $log 'waiting for an answer') {
        $how = 'left waiting for an approval'
    } else {
        Add-SmokeResult 'approval' 'FAIL' "no refusal and no waiting approval: $(Get-LastLine $log)"
        return
    }
    if ($Interactive) {
        Write-Host ''
        Write-Host 'Now approve it by hand: Indus opens the chat; answer the approval with yes, then type /quit.'
        $session = ''
        $waiting = Select-String -Path $log -Pattern 'indus -r ([^`]+)`' | Select-Object -First 1
        if ($waiting) { $session = $waiting.Matches[0].Groups[1].Value }
        Push-Location $repo
        if ($session -ne '') {
            & $script:Indus -r $session
        } else {
            & $script:Indus -m approve_or_decline chat -q 'please tidy the tree'
        }
        Pop-Location
        $answer = Read-Host 'Did Indus ask before running git reset --hard HEAD, and run it after you approved? [y/N]'
        if ($answer -match '^(y|yes)$') {
            Add-SmokeResult 'approval' 'PASS' "$how; asked and ran when a person approved"
        } else {
            Add-SmokeResult 'approval' 'FAIL' 'the person did not see the approval work'
        }
        return
    }
    Add-SmokeResult 'approval' 'PASS' "$how, nothing ran (interactive approval: -Interactive)"
}

function Step-Resume {
    # Through the daemon the session is a cli one; a --local one-shot is listed under oneshot.
    $list = Join-Path $script:Logs 'sessions.log'
    $oneshot = Join-Path $script:Logs 'sessions-oneshot.log'
    $null = Invoke-Indus 60 $list @('sessions', 'list', '--limit', '50')
    $null = Invoke-Indus 60 $oneshot @('sessions', 'list', '--limit', '50', '--source', 'oneshot')
    $row = @(Get-Content $list, $oneshot -ErrorAction SilentlyContinue) | Where-Object { $_ -like '*remember the blue kite*' } | Select-Object -First 1
    if (-not $row) {
        Add-SmokeResult 'resume' 'FAIL' "the first session is not listed: $(Get-LastLine $list)"
        return
    }
    $id = ([string]$row).Trim().Split(' ')[0]
    $log = Join-Path $script:Logs 'resume.log'
    $null = Invoke-Indus $SmokeTurnWait $log @('-r', $id, '-z', 'what did I ask') $script:Project
    if (Test-LogContains $log 'You said: what did I ask') {
        Add-SmokeResult 'resume' 'PASS' "session $id resumed"
    } else {
        Add-SmokeResult 'resume' 'FAIL' (Get-LastLine $log)
    }
}

function Step-Doctor {
    $log = Join-Path $script:Logs 'doctor.log'
    if ((Invoke-Indus 180 $log @('doctor')) -eq 0) {
        Add-SmokeResult 'doctor' 'PASS' (Get-LastLine $log)
    } else {
        Add-SmokeResult 'doctor' 'FAIL' (Get-LastLine $log)
    }
}

$script:Dashboard = $null
$script:DashboardUrl = ''

function Step-DashboardGate {
    $out = Join-Path $script:Logs 'dashboard.log'
    $script:Dashboard = Start-Process -FilePath $script:Indus -ArgumentList 'dashboard --port 0 --no-open' `
        -RedirectStandardInput $script:Empty -RedirectStandardOutput $out -RedirectStandardError "$out.err" `
        -WorkingDirectory $script:Work -NoNewWindow -PassThru
    $waited = 0
    while ($waited -lt $SmokeDashboardWait) {
        $line = Get-Content $out -ErrorAction SilentlyContinue | Where-Object { $_ -match '^Dashboard on (http://127\.0\.0\.1:[0-9]+)/' } | Select-Object -First 1
        if ($line -and ($line -match '^Dashboard on (http://127\.0\.0\.1:[0-9]+)/')) { $script:DashboardUrl = $Matches[1]; break }
        if ($script:Dashboard.HasExited) { break }
        Start-Sleep -Seconds 1
        $waited += 1
    }
    if ($script:DashboardUrl -eq '') {
        Add-SmokeResult 'dashboard-gate' 'FAIL' "the dashboard did not start: $(Get-LastLine $out)"
        return
    }
    $status = Get-SmokeHttp "$($script:DashboardUrl)/api/status"
    if ($status -ne 200) {
        Add-SmokeResult 'dashboard-gate' 'FAIL' "GET /api/status answered $status"
        return
    }
    $status = Get-SmokeHttp "$($script:DashboardUrl)/api/config"
    if ($status -ne 401) {
        Add-SmokeResult 'dashboard-gate' 'FAIL' "GET /api/config without the token answered $status, not 401"
        return
    }
    Add-SmokeResult 'dashboard-gate' 'PASS' "$($script:DashboardUrl): status 200, settings 401 without the token"
}

function Step-DashboardLogin {
    if ($script:DashboardUrl -eq '') {
        Add-SmokeResult 'dashboard-login' 'SKIP' 'the dashboard did not start'
        return
    }
    $status = Get-SmokeHttp "$($script:DashboardUrl)/"
    $token = ''
    if ($script:HttpBody -match '__INDUS_SESSION_TOKEN__="([^"]*)"') { $token = $Matches[1] }
    if ($status -ne 200 -or $token -eq '') {
        Add-SmokeResult 'dashboard-login' 'FAIL' "the page answered $status without a session token (see dashboard.log)"
        Stop-SmokeTree $script:Dashboard.Id
        $script:Dashboard = $null
        return
    }
    $status = Get-SmokeHttp "$($script:DashboardUrl)/api/config" @{ 'X-Indus-Session-Token' = $token }
    Stop-SmokeTree $script:Dashboard.Id
    $script:Dashboard = $null
    if ($status -ne 200) {
        Add-SmokeResult 'dashboard-login' 'FAIL' "the page's token was refused ($status)"
        return
    }
    Add-SmokeResult 'dashboard-login' 'PASS' "the page's token opens the API"
}

$script:SetupFile = ''
$script:AppDir = ''
$script:DesktopInstalled = $false

function Step-DesktopDownload {
    if ($Desktop -eq 'skip') {
        Add-SmokeResult 'desktop-download' 'SKIP' '-Desktop skip'
        return
    }
    if ($Desktop -ne 'auto') {
        $script:SetupFile = $Desktop
    } else {
        foreach ($name in Get-SmokeSetupNames) {
            $target = Join-Path $script:Work $name
            try {
                Invoke-WebRequest -Uri "$ReleaseUrl/$name" -OutFile $target -UseBasicParsing -TimeoutSec 900
                $script:SetupFile = $target
                break
            } catch { continue }
        }
    }
    if ($script:SetupFile -eq '' -or -not (Test-Path $script:SetupFile)) {
        Add-SmokeResult 'desktop-download' 'FAIL' "no desktop installer for $($env:PROCESSOR_ARCHITECTURE) on $ReleaseUrl"
        return
    }
    $log = Join-Path $script:Logs 'desktop-install.log'
    $code = Invoke-Bounded 600 $log $script:SetupFile @('/S')
    $script:AppDir = Join-Path $env:LOCALAPPDATA 'Programs\Indus'
    if ($code -ne 0 -or -not (Test-Path (Join-Path $script:AppDir 'Indus.exe'))) {
        Add-SmokeResult 'desktop-download' 'FAIL' "the per-user install did not finish ($code)"
        $script:AppDir = ''
        return
    }
    $script:DesktopInstalled = $true
    Add-SmokeResult 'desktop-download' 'PASS' "$(Split-Path $script:SetupFile -Leaf) installed into $($script:AppDir)"
}

function Step-Signature {
    if ($script:SetupFile -eq '' -or $script:AppDir -eq '') {
        Add-SmokeResult 'signature' 'SKIP' 'no desktop package'
        return
    }
    $bad = @()
    foreach ($file in @($script:SetupFile, (Join-Path $script:AppDir 'Indus.exe'))) {
        $signature = Get-AuthenticodeSignature -FilePath $file
        if ($signature.Status -ne 'Valid') { $bad += "$(Split-Path $file -Leaf): $($signature.Status)" }
    }
    if ($bad.Count -gt 0) {
        Add-SmokeResult 'signature' 'FAIL' "$($bad -join '; ') (unsigned until P23-16)"
        return
    }
    $signer = (Get-AuthenticodeSignature -FilePath $script:SetupFile).SignerCertificate.Subject
    Add-SmokeResult 'signature' 'PASS' "signed by $signer"
}

function Step-Licences {
    $missing = @()
    $notices = Join-Path $script:Checkout 'THIRD_PARTY_NOTICES.md'
    if (-not (Test-Path $notices) -or -not (Get-Content $notices -TotalCount 5 | Where-Object { $_ -eq '# Third-party notices' })) {
        $missing += 'checkout'
    }
    if ($script:AppDir -ne '') {
        $inside = Join-Path $script:AppDir 'resources\THIRD_PARTY_NOTICES.md'
        if (-not (Test-Path $inside) -or -not (Get-Content $inside -TotalCount 5 | Where-Object { $_ -eq '# Third-party notices' })) {
            $missing += 'desktop package'
        }
    }
    if ($missing.Count -gt 0) {
        Add-SmokeResult 'licences' 'FAIL' "THIRD_PARTY_NOTICES.md missing in: $($missing -join ', ')"
        return
    }
    # The desktop app has no licences screen of its own yet (follow-up in the rehearsal log).
    $where = 'the checkout'
    if ($script:AppDir -ne '') { $where = 'the checkout and the installed app' }
    Add-SmokeResult 'licences' 'PASS' "notices in $where (no in-app licences screen yet)"
}

function Step-DesktopLaunch {
    if ($script:AppDir -eq '') {
        Add-SmokeResult 'desktop-launch' 'SKIP' 'no desktop package'
        return
    }
    $app = Start-Process -FilePath (Join-Path $script:AppDir 'Indus.exe') -PassThru
    Start-Sleep -Seconds $SmokeDesktopWait
    if ($app.HasExited) {
        Add-SmokeResult 'desktop-launch' 'FAIL' "the app exited ($($app.ExitCode))"
        return
    }
    if ($Interactive) {
        $answer = Read-Host 'Did the desktop window open and reach the chat? [y/N]'
        Stop-SmokeTree $app.Id
        if ($answer -match '^(y|yes)$') {
            Add-SmokeResult 'desktop-launch' 'PASS' 'a person saw the chat'
        } else {
            Add-SmokeResult 'desktop-launch' 'FAIL' 'the person did not reach the chat'
        }
        return
    }
    Stop-SmokeTree $app.Id
    Add-SmokeResult 'desktop-launch' 'SKIP' "running after ${SmokeDesktopWait}s; chat not confirmed (use -Interactive)"
}

function Step-Uninstall {
    $log = Join-Path $script:Logs 'uninstall.log'
    if ((Invoke-Indus 300 $log @('uninstall', '-y')) -ne 0) {
        Add-SmokeResult 'uninstall' 'FAIL' (Get-LastLine $log)
        return
    }
    if ((Test-Path $script:Checkout) -or (Test-Path $script:Indus)) {
        Add-SmokeResult 'uninstall' 'FAIL' 'the code or the launcher is still there'
        return
    }
    $userPath = [string][Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -like "*$(Join-Path $script:IndusHome 'bin')*") {
        Add-SmokeResult 'uninstall' 'FAIL' "this user's PATH still names $(Join-Path $script:IndusHome 'bin')"
        return
    }
    if (-not (Test-Path (Join-Path $script:IndusHome 'state.db')) -or -not (Test-Path (Join-Path $script:IndusHome 'config.yaml'))) {
        Add-SmokeResult 'uninstall' 'FAIL' "the data was not kept in $($script:IndusHome)"
        return
    }
    Add-SmokeResult 'uninstall' 'PASS' 'code, launcher and PATH entry removed, sessions and settings kept'
}

function Stop-SmokeChildren {
    if ($null -ne $script:Dashboard) { Stop-SmokeTree $script:Dashboard.Id; $script:Dashboard = $null }
    if ($null -ne $script:Model) {
        try { $script:Model.StandardInput.Close() } catch { }
        if (-not $script:Model.WaitForExit(5000)) { Stop-SmokeTree $script:Model.Id }
        $script:Model = $null
    }
    if (Test-Path $script:Indus) { $null = Invoke-Indus 60 (Join-Path $script:Logs 'gateway-stop.log') @('gateway', 'stop') }
    if ($script:DesktopInstalled) {
        $uninstaller = Join-Path $script:AppDir 'Uninstall Indus.exe'
        if (Test-Path $uninstaller) { $null = Invoke-Bounded 300 (Join-Path $script:Logs 'desktop-uninstall.log') $uninstaller @('/S') }
        $script:DesktopInstalled = $false
    }
}

# -- the run --------------------------------------------------------------------------------------

if ($WorkDir -eq '') {
    $WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ('indus-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
} elseif ((Test-Path $WorkDir) -and @(Get-ChildItem -Force $WorkDir).Count -gt 0) {
    # The folder is removed at the end: never one that already holds something.
    [Console]::Error.WriteLine("-WorkDir must be a new or empty folder: $WorkDir")
    exit 2
}
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$script:Work = (Resolve-Path $WorkDir).Path
$script:Logs = Join-Path $script:Work 'logs'
$script:Project = Join-Path $script:Work 'project'
$script:IndusHome = Join-Path $script:Work 'indus-home'
$script:Checkout = Join-Path $script:IndusHome 'indus-agent'
$script:Indus = Join-Path $script:IndusHome 'bin\indus.cmd'
$script:Empty = Join-Path $script:Work 'empty.txt'
New-Item -ItemType Directory -Force -Path $script:Logs, $script:Project | Out-Null
[System.IO.File]::WriteAllText($script:Empty, '')
foreach ($name in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'INDUS_*' } | ForEach-Object { $_.Name })) {
    Remove-Item "Env:$name"
}
$env:INDUS_HOME = $script:IndusHome
$env:INDUS_TERMINAL_ID = 'smoke'
$env:NO_COLOR = '1'

Show-SmokePlan
$installed = $true
try {
    foreach ($step in $SmokeSteps) {
        if (-not $installed -and @('desktop-download', 'signature', 'desktop-launch') -notcontains $step) {
            Add-SmokeResult $step 'SKIP' 'the install failed'
            continue
        }
        switch ($step) {
            'install' { $installed = [bool](Step-Install) }
            'version' { Step-Version }
            'setup' { Step-Setup }
            'turn' { Step-Turn }
            'approval' { Step-Approval }
            'resume' { Step-Resume }
            'doctor' { Step-Doctor }
            'dashboard-gate' { Step-DashboardGate }
            'dashboard-login' { Step-DashboardLogin }
            'desktop-download' { Step-DesktopDownload }
            'signature' { Step-Signature }
            'licences' { Step-Licences }
            'desktop-launch' { Step-DesktopLaunch }
            'uninstall' { Step-Uninstall }
        }
    }
} finally {
    Stop-SmokeChildren
}

Write-Host ''
Write-Host "Summary ($SmokeOs, $Tag): $($script:Failed) failed"
foreach ($line in $script:Results) { Write-Host $line }
if ($script:Failed -eq 0 -and -not $Keep) {
    Remove-Item -Recurse -Force $script:Work -ErrorAction SilentlyContinue
    Write-Host 'Every step passed or was skipped; the work folder was removed.'
    exit 0
}
Write-Host "Logs: $($script:Logs)"
if ($script:Failed -gt 0) { exit 1 }
exit 0
