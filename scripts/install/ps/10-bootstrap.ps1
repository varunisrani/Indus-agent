
# ---------------------------------------------------------------------------------------------
# Windows bootstrap: pinned tools (portable Git, uv) verified by SHA-256, the desktop app check
# and its Start menu entry.
#
# Seams (script-scope scriptblocks a test replaces after dot-sourcing this file):
#   IndusDownload      param($Uri, $OutFile)       fetch one URL to a file; throws on failure
#   IndusStartProcess  param($FilePath, $ArgLine)  start a process; returns an object with
#                                                  Id, HasExited, ExitCode and WaitForExit(ms)
#   IndusKillTree      param($ProcessId)           kill a process and its children by pid
#   IndusNow           (no arguments)              the current time as a [DateTime]
#   IndusSleep         param($Milliseconds)        wait
# ---------------------------------------------------------------------------------------------

$script:IndusGitExtractLimitSeconds = 600
$script:IndusPollMilliseconds = 200

$script:IndusDownload = {
    param($Uri, $OutFile)
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing -ErrorAction Stop
}
$script:IndusStartProcess = {
    param($FilePath, $ArgLine)
    [System.Diagnostics.Process]::Start($FilePath, $ArgLine)
}
$script:IndusKillTree = {
    param($ProcessId)
    Invoke-IndusNative { taskkill.exe /T /F /PID $ProcessId 2>&1 } | Out-Null
}
$script:IndusNow = { [DateTime]::UtcNow }
$script:IndusSleep = {
    param($Milliseconds)
    Start-Sleep -Milliseconds $Milliseconds
}

# The machine's architecture (an x64 PowerShell on Windows on Arm must still fetch arm64 tools).
function Get-IndusArch {
    $machine = ''
    try {
        $machine = [string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' -ErrorAction Stop).PROCESSOR_ARCHITECTURE
    } catch {
        $machine = [string]$env:PROCESSOR_ARCHITECTURE
    }
    if ($machine -eq 'ARM64') { return 'arm64' }
    if ($machine -eq 'AMD64' -or $machine -eq 'x86' -or $machine -eq '') { return 'x64' }
    return $machine.ToLowerInvariant()
}

function Get-IndusTarget {
    return 'windows-' + (Get-IndusArch)
}

function Get-IndusPins {
    if (-not $script:IndusPinsData) { $script:IndusPinsData = $script:IndusPinsJson | ConvertFrom-Json }
    return $script:IndusPinsData
}

# The one reader of the pins table: @{ Version; Url; MirrorUrl; Sha256 } for a tool and target,
# or $null when the tool has no pin for that target.
function Get-IndusPin([string]$Tool, [string]$Target) {
    $pins = Get-IndusPins
    $entry = $pins.$Tool
    if (-not $entry) { return $null }
    $file = $entry.targets.$Target
    if (-not $file) { return $null }
    $mirror = [string]$file.mirror
    if (-not $mirror) { $mirror = [string]$file.mirror_url }
    return @{
        Version   = [string]$entry.version
        Url       = [string]$file.url
        MirrorUrl = $mirror
        Sha256    = ([string]$file.sha256).ToLowerInvariant()
    }
}

function Get-IndusFileSha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-IndusTempDir([string]$Label) {
    return (Join-Path ([IO.Path]::GetTempPath()) ('indus-' + $Label + '-' + $PID + '-' + (Get-Random)))
}

# What kind of failure a download raised: 'network' (DNS, connect, timeout, reset, or an HTTP
# error status: the cases curl reports as 5, 6, 7, 18, 22, 28, 52, 55, 56), 'tls' (a trust or
# handshake failure: never a routing problem) or 'other'. Only 'network' may try the mirror.
function Get-IndusDownloadFailureKind($ErrorRecord) {
    $ex = $ErrorRecord
    if ($ErrorRecord -is [System.Management.Automation.ErrorRecord]) { $ex = $ErrorRecord.Exception }
    $seen = 0
    while ($ex -and $seen -lt 6) {
        $typeName = $ex.GetType().FullName
        if ($typeName -eq 'System.Security.Authentication.AuthenticationException') { return 'tls' }
        if ($ex -is [System.Net.WebException]) {
            $status = [string]$ex.Status
            if ($status -in @('TrustFailure', 'SecureChannelFailure')) { return 'tls' }
            if ($status -in @('NameResolutionFailure', 'ProxyNameResolutionFailure', 'ConnectFailure',
                    'Timeout', 'ConnectionClosed', 'KeepAliveFailure', 'ReceiveFailure', 'SendFailure',
                    'PipelineFailure', 'ProtocolError', 'RequestCanceled')) { return 'network' }
            return 'other'
        }
        if ($typeName -eq 'Microsoft.PowerShell.Commands.HttpResponseException') { return 'network' }
        if ($typeName -eq 'System.Net.Http.HttpRequestException') {
            if ($ex.InnerException -and $ex.InnerException.GetType().FullName -eq 'System.Security.Authentication.AuthenticationException') { return 'tls' }
            return 'network'
        }
        if ($typeName -in @('System.Net.Sockets.SocketException', 'System.TimeoutException',
                'System.Threading.Tasks.TaskCanceledException')) { return 'network' }
        $ex = $ex.InnerException
        $seen += 1
    }
    return 'other'
}

# Downloads a pinned file. The mirror is tried only after a network-class failure of the upstream
# URL; whichever origin answered, the bytes must match the pinned SHA-256 or the file is removed
# and the install stops (download_digest_mismatch) without trying anything else.
function Invoke-IndusVerifiedDownload {
    param([string]$Url, [string]$MirrorUrl, [string]$Sha256, [string]$OutFile)
    if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$') {
        Stop-IndusStage "no SHA-256 is pinned for $Url; refusing to download it" 'download_failed'
    }
    $candidates = @($Url)
    if ($MirrorUrl -and $MirrorUrl -ne $Url) { $candidates += $MirrorUrl }
    $lastProblem = ''
    foreach ($candidate in $candidates) {
        if (-not $candidate.StartsWith('https://')) {
            Stop-IndusStage "refusing a download that is not https: $candidate" 'download_failed'
        }
        if (Test-Path -LiteralPath $OutFile) { Remove-Item -LiteralPath $OutFile -Force }
        try {
            & $script:IndusDownload $candidate $OutFile
        } catch {
            $kind = Get-IndusDownloadFailureKind $_
            if ($kind -ne 'network') {
                Stop-IndusStage "download of $candidate failed: $($_.Exception.Message)" 'download_failed'
            }
            $lastProblem = $_.Exception.Message
            Write-IndusWarn "could not reach $candidate ($lastProblem)"
            continue
        }
        if (-not (Test-Path -LiteralPath $OutFile)) {
            $lastProblem = 'no file was written'
            continue
        }
        $digest = Get-IndusFileSha256 $OutFile
        if ($digest -eq $Sha256.ToLowerInvariant()) { return }
        Remove-Item -LiteralPath $OutFile -Force -ErrorAction SilentlyContinue
        Stop-IndusStage "download digest mismatch for $candidate (expected $Sha256, got $digest)" 'download_digest_mismatch'
    }
    Stop-IndusStage ("could not download " + ($candidates -join ' or ') + $(if ($lastProblem) { ": $lastProblem" } else { '' })) 'download_failed'
}

# Runs a program with a time limit, polling on the injectable clock. Returns @{ TimedOut;
# ExitCode }. On overrun the program and its children are killed by pid.
function Invoke-IndusBoundedProcess {
    param([string]$FilePath, [string]$ArgLine, [int]$LimitSeconds)
    $proc = & $script:IndusStartProcess $FilePath $ArgLine
    if (-not $proc) { return @{ TimedOut = $false; ExitCode = -1 } }
    $started = & $script:IndusNow
    while (-not $proc.HasExited) {
        $elapsed = ((& $script:IndusNow) - $started).TotalSeconds
        if ($elapsed -ge $LimitSeconds) {
            & $script:IndusKillTree $proc.Id | Out-Null
            try { [void]$proc.WaitForExit(10000) } catch { }
            return @{ TimedOut = $true; ExitCode = $null }
        }
        & $script:IndusSleep $script:IndusPollMilliseconds | Out-Null
    }
    return @{ TimedOut = $false; ExitCode = $proc.ExitCode }
}

function Get-IndusToolsDir {
    return (Join-Path $script:IndusPaths.Home 'tools')
}

# Pinned portable Git for this machine: <home>\tools\git-<version>\cmd\git.exe. Downloaded,
# verified, extracted by its own self-extractor under the 600 s limit into a temp dir, and moved
# into place only when complete.
function Get-IndusGit {
    $target = Get-IndusTarget
    $pin = Get-IndusPin 'git_for_windows' $target
    if (-not $pin) { Stop-IndusStage "no pinned Git for $target; this machine is not supported" 'unsupported_platform' }
    $entry = Join-Path (Get-IndusToolsDir) ('git-' + $pin.Version)
    $gitExe = Join-Path (Join-Path $entry 'cmd') 'git.exe'
    if (Test-Path -LiteralPath $gitExe) { return $gitExe }
    Write-IndusInfo "Installing Git $($pin.Version) ($target)"
    $work = Get-IndusTempDir 'git'
    try {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $archive = Join-Path $work 'portable-git.7z.exe'
        Invoke-IndusVerifiedDownload -Url $pin.Url -MirrorUrl $pin.MirrorUrl -Sha256 $pin.Sha256 -OutFile $archive
        $unpacked = Join-Path $work 'unpacked'
        New-Item -ItemType Directory -Force -Path $unpacked | Out-Null
        Unblock-File -LiteralPath $archive -ErrorAction SilentlyContinue
        $run = Invoke-IndusBoundedProcess -FilePath $archive -ArgLine ('-o"' + $unpacked + '" -y') -LimitSeconds $script:IndusGitExtractLimitSeconds
        if ($run.TimedOut) {
            Stop-IndusStage "the Git self-extractor did not finish within $($script:IndusGitExtractLimitSeconds) s and was stopped" 'git_extract_failed'
        }
        if ($run.ExitCode) {
            Stop-IndusStage "the Git self-extractor exited $($run.ExitCode) (a full disk, a path that is too long or an antivirus lock are the usual causes)" 'git_extract_failed'
        }
        if (-not (Test-Path -LiteralPath (Join-Path (Join-Path $unpacked 'cmd') 'git.exe'))) {
            Stop-IndusStage 'git.exe is missing from the extracted Git' 'git_extract_failed'
        }
        if (Test-Path -LiteralPath $entry) { Remove-Item -LiteralPath $entry -Recurse -Force }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $entry) | Out-Null
        Move-Item -LiteralPath $unpacked -Destination $entry
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    return $gitExe
}

# Puts the pinned Git first on this process's PATH (never the user PATH). Every -Stage run is a
# new process, so each stage that runs git calls this.
function Use-IndusGit {
    $gitExe = Get-IndusGit
    $cmdDir = Split-Path -Parent $gitExe
    $entries = @($env:Path -split ';' | Where-Object { $_ })
    $present = $false
    foreach ($item in $entries) { if (Test-IndusSamePath $item $cmdDir) { $present = $true } }
    if (-not $present) { $env:Path = $cmdDir + ';' + $env:Path }
    return $gitExe
}

function Test-IndusUvRuns([string]$Path) {
    $global:LASTEXITCODE = 0
    $said = Invoke-IndusNative { & $Path --version 2>$null }
    return (-not $LASTEXITCODE) -and [bool]$said
}

# Pinned uv for this machine: <home>\tools\uv-<version>-<target>\uv.exe, verified and run once.
function Get-IndusUv {
    $target = Get-IndusTarget
    $pin = Get-IndusPin 'uv' $target
    if (-not $pin) { Stop-IndusStage "no pinned uv for $target; this machine is not supported" 'unsupported_platform' }
    $entry = Join-Path (Get-IndusToolsDir) ('uv-' + $pin.Version + '-' + $target)
    $uvExe = Join-Path $entry 'uv.exe'
    if (Test-Path -LiteralPath $uvExe) {
        if (Test-IndusUvRuns $uvExe) { return $uvExe }
        Write-IndusWarn 'the cached uv does not run; fetching it again'
        Remove-Item -LiteralPath $uvExe -Force
    }
    Write-IndusInfo "Installing uv $($pin.Version) ($target)"
    $work = Get-IndusTempDir 'uv'
    try {
        New-Item -ItemType Directory -Force -Path $work | Out-Null
        $zip = Join-Path $work 'uv.zip'
        Invoke-IndusVerifiedDownload -Url $pin.Url -MirrorUrl $pin.MirrorUrl -Sha256 $pin.Sha256 -OutFile $zip
        $unpacked = Join-Path $work 'unpacked'
        Expand-Archive -LiteralPath $zip -DestinationPath $unpacked -Force
        $found = Get-ChildItem -LiteralPath $unpacked -Filter 'uv.exe' -Recurse | Select-Object -First 1
        if (-not $found) { Stop-IndusStage 'uv.exe is missing from the downloaded archive' 'uv_unusable' }
        New-Item -ItemType Directory -Force -Path $entry | Out-Null
        Move-Item -LiteralPath $found.FullName -Destination $uvExe -Force
        $companion = Get-ChildItem -LiteralPath $unpacked -Filter 'uvx.exe' -Recurse | Select-Object -First 1
        if ($companion) { Move-Item -LiteralPath $companion.FullName -Destination (Join-Path $entry 'uvx.exe') -Force }
    } finally {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-IndusUvRuns $uvExe)) { Stop-IndusStage 'the pinned uv was installed but does not run on this machine' 'uv_unusable' }
    return $uvExe
}

# Windows refuses paths over 260 characters unless long paths are switched on; git checkouts
# get core.longpaths, and a long install path without the system switch gets a warning.
function Test-IndusLongPathsEnabled {
    try {
        $value = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -ErrorAction Stop).LongPathsEnabled
        return ($value -eq 1)
    } catch {
        return $false
    }
}

function Write-IndusLongPathAdvice {
    $codeDir = [string]$script:IndusPaths.InstallDir
    if ($codeDir.Length -gt 80 -and -not (Test-IndusLongPathsEnabled)) {
        Write-IndusWarn 'Windows long paths are off and the install path is long; some files may fail to unpack.'
        Write-IndusWarn 'Turn them on (as administrator): Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1'
    }
}

# The desktop app built in this checkout, or $null.
function Find-IndusDesktopApp {
    $release = Join-Path (Join-Path (Join-Path $script:IndusPaths.InstallDir 'apps') 'desktop') 'release'
    foreach ($folder in @('win-unpacked', 'win-arm64-unpacked')) {
        $candidate = Join-Path (Join-Path $release $folder) 'Indus.exe'
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

# Best effort: a shortcut that cannot be made only warns.
function New-IndusStartMenuShortcut([string]$TargetExe) {
    try {
        $programs = [Environment]::GetFolderPath('Programs')
        if (-not $programs) { Write-IndusWarn 'no Start menu folder; skipping the shortcut'; return }
        if (-not (Test-Path -LiteralPath $programs)) { New-Item -ItemType Directory -Force -Path $programs | Out-Null }
        $link = Join-Path $programs 'Indus.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($link)
        $shortcut.TargetPath = $TargetExe
        $shortcut.WorkingDirectory = Split-Path -Parent $TargetExe
        $shortcut.Description = 'Indus Agent'
        $shortcut.Save()
        Write-IndusOk "Start menu entry: $link"
    } catch {
        Write-IndusWarn "could not create the Start menu entry: $($_.Exception.Message)"
    }
}

# -IncludeDesktop (and -Stage desktop): the desktop build must exist; then the Start menu entry.
function Invoke-IndusDesktop {
    $app = Find-IndusDesktopApp
    if (-not $app) {
        Stop-IndusStage "no desktop app under $($script:IndusPaths.InstallDir)\apps\desktop\release (win-unpacked or win-arm64-unpacked); build it first" 'products_build_failed'
    }
    Write-IndusOk "Desktop app: $app"
    New-IndusStartMenuShortcut $app
}
