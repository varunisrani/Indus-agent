# Indus Agent installer for Windows (Windows PowerShell 5.1 and PowerShell 7).
#
# From PowerShell:      irm <release>/install.ps1 | iex
# With options:         & ([scriptblock]::Create((irm <release>/install.ps1))) -Channel canary
# From Command Prompt:  install.cmd (runs this script through PowerShell)
#
# Installs the runtime the desktop app uses: a git checkout, pinned uv and Python, the locked
# environment and the `indus` command on the user PATH. On Windows the CLI, TUI and gateway are
# supported through WSL2; run install.sh inside WSL for those.
#
# Stage protocol (shared with install.sh and driven by the desktop app):
#   -Manifest               print the stage list as one JSON line
#   -Stage NAME [-Json]     run one stage; with -Json print one result frame
#   -NonInteractive         skip the stages that need a person (alias -SkipSetup)
# POSIX spellings (--stage, --indus-home, --non-interactive, --dir, ...) are accepted too.
#
# This file is generated from scripts/install/ps/*.ps1, stages.json and pins.json. Edit those.
# Dot-sourcing it (. .\install.ps1) defines the functions and runs nothing.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$Branch = "",
    [string]$Commit = "",
    [string]$Channel = "",
    [string]$IndusHome = "",
    [string]$InstallDir = "",
    [switch]$Manifest,
    [string]$Stage = "",
    [switch]$Json,
    [switch]$NonInteractive,
    [switch]$SkipSetup,
    [switch]$IncludeDesktop,
    [switch]$SkipBrowser,
    [switch]$SkipComputerUse,
    [switch]$ShowResolvedPaths,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PosixArgs
)

# A dot-source sees InvocationName '.'; only the definitions below may enter the caller's session.
$script:IndusDotSourced = ($MyInvocation.InvocationName -eq '.')
# Only a script file owns its process and may `exit`; text run through iex or a scriptblock runs
# inside the caller's session, where `exit` would close the person's window.
$script:IndusRunAsFile = [bool]{}.File

# The options every function reads. Parameters bind in the script's own scope, which is not the
# `script:` scope when this text runs as a scriptblock, so they are copied here once.
$script:IndusOpts = @{
    Branch            = $Branch
    Commit            = $Commit
    Channel           = $Channel
    IndusHome         = $IndusHome
    InstallDir        = $InstallDir
    Manifest          = [bool]$Manifest
    Stage             = $Stage
    Json              = [bool]$Json
    NonInteractive    = ([bool]$NonInteractive -or [bool]$SkipSetup)
    IncludeDesktop    = [bool]$IncludeDesktop
    SkipBrowser       = [bool]$SkipBrowser
    SkipComputerUse   = [bool]$SkipComputerUse
    ShowResolvedPaths = [bool]$ShowResolvedPaths
    Verbose           = ($VerbosePreference -ne 'SilentlyContinue')
    Help              = $false
}
$script:IndusRest = @($PosixArgs | Where-Object { $null -ne $_ })

# @@INDUS_EMBED@@

$script:IndusStages = $script:IndusStagesJson | ConvertFrom-Json
$script:IndusPinsData = $null
$script:IndusPaths = $null
$script:IndusReceiptWritten = $false
$script:IndusRunId = ''
$script:IndusStartedAt = ''
$script:IndusCurrentStage = ''
$script:IndusFailureClass = ''
$script:IndusNativeError = ''
$script:IndusLongProfileRoot = $null
$script:IndusLastResolver = 'none'
$script:IndusRewrites = @{}
$script:IndusExitCode = 1

$script:IndusChannels = @('stable', 'canary', 'main')
$script:IndusValueFlags = @{
    branch     = 'Branch'
    commit     = 'Commit'
    channel    = 'Channel'
    dir        = 'InstallDir'
    installdir = 'InstallDir'
    indushome  = 'IndusHome'
    stage      = 'Stage'
}
$script:IndusSwitchFlags = @{
    manifest          = 'Manifest'
    json              = 'Json'
    noninteractive    = 'NonInteractive'
    skipsetup         = 'NonInteractive'
    includedesktop    = 'IncludeDesktop'
    skipbrowser       = 'SkipBrowser'
    skipcomputeruse   = 'SkipComputerUse'
    showresolvedpaths = 'ShowResolvedPaths'
    verbose           = 'Verbose'
    h                 = 'Help'
    help              = 'Help'
}

# ---------------------------------------------------------------------------------------------
# Output. Machine output (manifest, frames, resolved paths) goes straight to the process's stdout,
# never into the PowerShell pipeline. ASCII only: Windows PowerShell 5.1 reads a file without a
# byte order mark in the ANSI code page.
# ---------------------------------------------------------------------------------------------

function Write-IndusJsonLine([string]$Text) {
    [Console]::Out.WriteLine($Text)
    [Console]::Out.Flush()
}

function Write-IndusInfo([string]$Message) { Write-Host "-> $Message" }
function Write-IndusOk([string]$Message) { Write-Host "[ok] $Message" }
function Write-IndusWarn([string]$Message) { Write-Host "[!] $Message" }
function Write-IndusError([string]$Message) { [Console]::Error.WriteLine("[x] $Message") }

function Write-IndusUsage {
    $lines = @(
        'Usage: install.ps1 [options]',
        '  -Channel stable|canary|main   which line to install (env INDUS_INSTALL_CHANNEL)',
        '  -Branch NAME                  branch for the main channel (default main)',
        '  -Commit SHA                   pin a commit on that branch',
        '  -IndusHome PATH               the Indus home (default %LOCALAPPDATA%\indus)',
        '  -InstallDir PATH              where the code goes (default <home>\indus-agent)',
        '  -Manifest                     print the stage list as JSON',
        '  -Stage NAME [-Json]           run one stage',
        '  -NonInteractive, -SkipSetup   skip the stages that need a person',
        '  -IncludeDesktop               also check the desktop app and add a Start menu entry',
        '  -SkipBrowser                  leave out the browser tools',
        '  -SkipComputerUse              leave out the computer-use tools',
        '  -ShowResolvedPaths            print the paths this install would use, as JSON',
        'POSIX spellings such as --stage, --indus-home and --non-interactive work too.'
    )
    foreach ($line in $lines) { Write-Host $line }
}

function Get-IndusUtcStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH':'mm':'ss'Z'", [Globalization.CultureInfo]::InvariantCulture)
}

function Get-IndusCompactStamp {
    return (Get-Date).ToUniversalTime().ToString("yyyyMMdd'T'HHmmss'Z'", [Globalization.CultureInfo]::InvariantCulture)
}

function Test-IndusWindows {
    return [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
}

# ---------------------------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------------------------

# Applies the POSIX (and stray PowerShell-style) spellings onto the options. Returns $null, or
# @{ Code; Message } for a usage problem: 2 when a value is missing, 1 for an unknown flag.
function ConvertFrom-IndusArgs {
    param([string[]]$Tokens)
    $list = @($Tokens | Where-Object { $null -ne $_ })
    $at = 0
    while ($at -lt $list.Count) {
        $token = [string]$list[$at]
        if (-not $token.StartsWith('-')) {
            return @{ Code = 1; Message = "unexpected argument: $token" }
        }
        $body = $token.TrimStart('-')
        $inline = $null
        $eq = $body.IndexOf('=')
        if ($eq -ge 0) {
            $inline = $body.Substring($eq + 1)
            $body = $body.Substring(0, $eq)
        }
        $body = $body.TrimEnd(':')
        $key = ($body -replace '[-_]', '').ToLowerInvariant()
        if ($script:IndusValueFlags.ContainsKey($key)) {
            $value = $inline
            if ($null -eq $value) {
                if (($at + 1) -ge $list.Count -or ([string]$list[$at + 1]).StartsWith('-')) {
                    return @{ Code = 2; Message = "$token needs a value" }
                }
                $value = [string]$list[$at + 1]
                $at += 1
            }
            $script:IndusOpts[$script:IndusValueFlags[$key]] = $value
        } elseif ($script:IndusSwitchFlags.ContainsKey($key) -and $null -eq $inline) {
            $script:IndusOpts[$script:IndusSwitchFlags[$key]] = $true
        } else {
            return @{ Code = 1; Message = "unknown option: $token" }
        }
        $at += 1
    }
    return $null
}

# The channel this run was asked for ('' when none): -Channel, else INDUS_INSTALL_CHANNEL.
# Returns an error message for a channel that does not exist.
function Resolve-IndusChannel {
    $asked = [string]$script:IndusOpts.Channel
    if (-not $asked -and $env:INDUS_INSTALL_CHANNEL) { $asked = [string]$env:INDUS_INSTALL_CHANNEL }
    $asked = $asked.Trim()
    if ($asked -and ($script:IndusChannels -cnotcontains $asked)) {
        return "the channel must be stable, canary or main, not '$asked'"
    }
    $script:IndusOpts.Channel = $asked
    return $null
}

function Test-IndusNonInteractive {
    return [bool]$script:IndusOpts.NonInteractive
}

# A person can answer questions: not -NonInteractive and the console input is not redirected.
function Test-IndusConsole {
    if (Test-IndusNonInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected } catch { return $false }
}

# ---------------------------------------------------------------------------------------------
# Paths. Windows can expose a profile folder through an 8.3 alias (C:\Users\FIRST~1.LAS) in TEMP,
# LOCALAPPDATA and the rest; several cmdlets then fail to find paths below it. Every
# profile-rooted variable is expanded to its long form before anything reads it.
# ---------------------------------------------------------------------------------------------

function Get-IndusLongProfileRoot {
    if ($null -ne $script:IndusLongProfileRoot) { return $script:IndusLongProfileRoot }
    $script:IndusLongProfileRoot = ''
    $candidates = @(
        [Environment]::GetEnvironmentVariable('USERPROFILE'),
        [Environment]::GetFolderPath('UserProfile')
    )
    foreach ($anchor in @($candidates)) {
        if ($anchor -and $env:USERNAME) {
            $parentDir = Split-Path -Parent $anchor.TrimEnd('\', '/')
            if ($parentDir) { $candidates += (Join-Path $parentDir $env:USERNAME) }
        }
    }
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        $trimmed = $candidate.TrimEnd('\', '/')
        if (-not $trimmed -or $trimmed -match '~\d') { continue }
        try {
            if (Test-Path -LiteralPath $trimmed -PathType Container) {
                $script:IndusLongProfileRoot = $trimmed
                break
            }
        } catch {
            continue
        }
    }
    return $script:IndusLongProfileRoot
}

# Rebuilds $Path on the long profile root when its aliased component is the profile folder itself;
# any other path (a short name on another volume) comes back unchanged.
function Expand-IndusShortProfileRoot([string]$Path) {
    $longRoot = Get-IndusLongProfileRoot
    if (-not $longRoot) { return $Path }
    $usersDir = Split-Path -Parent $longRoot
    if (-not $usersDir) { return $Path }
    $node = $Path
    $tail = ''
    while ($node -and ($node -match '~\d')) {
        $leaf = Split-Path -Leaf $node
        $above = Split-Path -Parent $node
        if (-not $above) { return $Path }
        if ($leaf -match '~\d') {
            if ($above -ne $usersDir) { return $Path }
            if ($tail) { return (Join-Path $longRoot $tail) }
            return $longRoot
        }
        if ($tail) { $tail = Join-Path $leaf $tail } else { $tail = $leaf }
        $node = $above
    }
    return $Path
}

function ConvertTo-IndusLongPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return $Path }
    if ($Path -notmatch '~\d') { return $Path }
    try {
        if (-not ('IndusInstall.LongPath' -as [type])) {
            Add-Type -Namespace 'IndusInstall' -Name 'LongPath' -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
public static extern int GetLongPathNameW(string shortPath, System.Text.StringBuilder longPath, int size);
'@
        }
        $buffer = New-Object System.Text.StringBuilder 4096
        $length = [IndusInstall.LongPath]::GetLongPathNameW($Path, $buffer, $buffer.Capacity)
        if ($length -gt $buffer.Capacity) {
            $buffer = New-Object System.Text.StringBuilder $length
            $length = [IndusInstall.LongPath]::GetLongPathNameW($Path, $buffer, $buffer.Capacity)
        }
        if ($length -gt 0) {
            $expanded = $buffer.ToString()
            if ($expanded -and $expanded -notmatch '~\d') {
                $script:IndusLastResolver = 'kernel32'
                return $expanded
            }
        }
    } catch {
        # Not Windows, or P/Invoke refused by policy: rebuild from the profile root instead.
    }
    $rebuilt = Expand-IndusShortProfileRoot $Path
    if ($rebuilt -ne $Path) { $script:IndusLastResolver = 'profile-root' } else { $script:IndusLastResolver = 'none' }
    return $rebuilt
}

function Set-IndusLongEnvPaths {
    $script:IndusRewrites = @{}
    foreach ($name in @('TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE')) {
        $current = [Environment]::GetEnvironmentVariable($name)
        if (-not $current) { continue }
        $expanded = ConvertTo-IndusLongPath $current
        if ($expanded -and $expanded -ne $current) {
            Set-Item -Path "Env:$name" -Value $expanded
            $script:IndusRewrites[$name] = $expanded
        }
    }
}

function Get-IndusDefaultHome {
    if ($env:INDUS_HOME) { return [string]$env:INDUS_HOME }
    $local = [string]$env:LOCALAPPDATA
    if (-not $local) {
        $profileDir = [Environment]::GetFolderPath('UserProfile')
        $local = Join-Path (Join-Path $profileDir 'AppData') 'Local'
    }
    return (Join-Path $local 'indus')
}

# Resolves the home and the install dir once (after the env is long-form) and keeps the report
# -ShowResolvedPaths prints. Touches nothing on disk.
function Initialize-IndusPaths {
    Set-IndusLongEnvPaths
    if ($script:IndusOpts.IndusHome) {
        $homeDir = ConvertTo-IndusLongPath ([string]$script:IndusOpts.IndusHome)
    } else {
        $homeDir = ConvertTo-IndusLongPath (Get-IndusDefaultHome)
    }
    if ($script:IndusOpts.InstallDir) {
        $codeDir = ConvertTo-IndusLongPath ([string]$script:IndusOpts.InstallDir)
    } else {
        $codeDir = Join-Path $homeDir 'indus-agent'
    }
    $env:INDUS_HOME = $homeDir
    $script:IndusPaths = @{
        Home       = $homeDir
        InstallDir = $codeDir
        Report     = [ordered]@{
            long_profile_root = (Get-IndusLongProfileRoot)
            normalized        = $script:IndusRewrites
            resolver          = $script:IndusLastResolver
            temp              = [string]$env:TEMP
            indus_home        = $homeDir
            install_dir       = $codeDir
        }
    }
}

function Test-IndusSamePath([string]$Left, [string]$Right) {
    $a = $Left.TrimEnd('\', '/')
    $b = $Right.TrimEnd('\', '/')
    return [string]::Equals($a, $b, [StringComparison]::OrdinalIgnoreCase)
}

function Test-IndusPathInside([string]$Inner, [string]$Outer) {
    $in = $Inner.TrimEnd('\', '/')
    $out = $Outer.TrimEnd('\', '/')
    if ([string]::Equals($in, $out, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    foreach ($separator in @('\', '/')) {
        if ($in.StartsWith($out + $separator, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

# A home equal to or inside the install dir would put <home>\tools inside the checkout: refused
# before anything downloads.
function Assert-IndusLayout {
    if (-not $script:IndusPaths) { Initialize-IndusPaths }
    $homeDir = $script:IndusPaths.Home
    $codeDir = $script:IndusPaths.InstallDir
    if (Test-IndusPathInside $homeDir $codeDir) {
        Stop-IndusStage "the Indus home ($homeDir) cannot be the install directory or live inside it ($codeDir); pass a separate -IndusHome or -InstallDir" 'filesystem_error'
    }
}

function Get-IndusLogPath {
    return (Join-Path (Join-Path $script:IndusPaths.Home 'logs') 'install.log')
}

# ---------------------------------------------------------------------------------------------
# Embedded tables
# ---------------------------------------------------------------------------------------------

function Get-IndusStageDef([string]$Name) {
    foreach ($def in @($script:IndusStages.stages)) {
        if ($def.name -ceq $Name) { return $def }
    }
    return $null
}

function Get-IndusManifestJson {
    $list = @()
    foreach ($def in @($script:IndusStages.stages)) {
        $title = [string]$def.title
        if ($script:IndusOpts.IncludeDesktop -and $def.title_with_desktop) { $title = [string]$def.title_with_desktop }
        $list += [ordered]@{
            name             = [string]$def.name
            title            = $title
            category         = [string]$def.category
            needs_user_input = [bool]$def.needs_user_input
        }
    }
    $manifest = [ordered]@{ protocol_version = [int]$script:IndusStages.protocol_version; stages = $list }
    return ($manifest | ConvertTo-Json -Depth 5 -Compress)
}

# The release origin (owner decision D-16.690 pending): the one constant is pins.json
# release.repository ("owner/name"); clone, API, release and shim URLs all derive from it.
function Get-IndusRepository {
    $pins = $script:IndusPinsJson | ConvertFrom-Json
    $named = ([string]$pins.release.repository).Trim().TrimEnd('/')
    if ($named.StartsWith('https://')) { return $named }
    return 'https://github.com/' + $named
}

function Get-IndusCloneUrl {
    if ($env:INDUS_REPO_URL) { return [string]$env:INDUS_REPO_URL }
    return (Get-IndusRepository) + '.git'
}

# ---------------------------------------------------------------------------------------------
# Failures, frames and the receipt
# ---------------------------------------------------------------------------------------------

# Throws; the entry points own reporting and the exit code. $Class is the receipt's closed
# failure class.
function Stop-IndusStage([string]$Message, [string]$Class = 'other') {
    $script:IndusFailureClass = $Class
    throw $Message
}

# The class for a ladder stage that threw. A native child that Ctrl-C ended (130, or Windows
# STATUS_CONTROL_C_EXIT) is an interruption whatever the stage said.
function Get-IndusFailureClass {
    if ($global:LASTEXITCODE -in @(130, -1073741510, 3221225786)) { return 'interrupted' }
    if ($script:IndusFailureClass) { return $script:IndusFailureClass }
    return 'other'
}

function Write-IndusFrame([bool]$Ok, [string]$Name, [bool]$Skipped, [string]$Reason = '') {
    $frame = [ordered]@{ ok = $Ok; stage = $Name; skipped = $Skipped }
    if ($Reason) { $frame.reason = $Reason }
    Write-IndusJsonLine ($frame | ConvertTo-Json -Compress)
}

# One local receipt per full-ladder run; never uploaded. The first writer wins: a second call in
# this process (the finally block's 'interrupted') and an existing receipt file are both no-ops.
# Best effort: a receipt that cannot be written never fails the install. Returns $true when written.
function Write-IndusReceipt([string]$Outcome, [string]$FailedStage = '', [string]$FailureClass = '') {
    if ($script:IndusReceiptWritten) { return $false }
    $script:IndusReceiptWritten = $true
    $tmpFile = $null
    try {
        if (-not $script:IndusRunId) { $script:IndusRunId = [guid]::NewGuid().ToString('N') }
        if (-not $script:IndusStartedAt) { $script:IndusStartedAt = Get-IndusUtcStamp }
        $receiptDir = Join-Path (Join-Path $script:IndusPaths.Home 'logs') 'install_receipts'
        New-Item -ItemType Directory -Force -Path $receiptDir | Out-Null
        $final = Join-Path $receiptDir ($script:IndusRunId + '.json')
        if (Test-Path -LiteralPath $final) { return $false }
        $record = [ordered]@{ id = $script:IndusRunId; installer = 'ps1'; outcome = $Outcome }
        if ($FailedStage) { $record.failed_stage = $FailedStage }
        if ($FailureClass) { $record.failure_class = $FailureClass }
        $record.started_at = $script:IndusStartedAt
        $record.finished_at = Get-IndusUtcStamp
        $tmpFile = Join-Path $receiptDir ('.' + $script:IndusRunId + '.tmp')
        [IO.File]::WriteAllText($tmpFile, ($record | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $tmpFile -Destination $final -ErrorAction Stop
        $tmpFile = $null
        return $true
    } catch {
        if ($tmpFile) { Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue }
        return $false
    }
}

# ---------------------------------------------------------------------------------------------
# Native commands. Windows PowerShell 5.1 turns a native tool's redirected stderr into an error
# record that 'Stop' makes fatal; native calls run with the preference relaxed here, and the
# caller judges $LASTEXITCODE. The helpers below use unusual local names because the blocks they
# run resolve variables through their scope first.
# ---------------------------------------------------------------------------------------------

function Invoke-IndusNative {
    param([scriptblock]$IndusNativeBlock, [switch]$Utf8)
    $ErrorActionPreference = 'Continue'
    if (-not $Utf8) {
        & $IndusNativeBlock
        return
    }
    $nvPrevious = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        & $IndusNativeBlock
    } finally {
        [Console]::OutputEncoding = $nvPrevious
    }
}

# The cause line of a failed command: the first "fatal:"/"error:" line, else the last line.
function Select-IndusErrorLine($Lines) {
    $slPick = $null
    $slLast = $null
    foreach ($slRaw in @($Lines)) {
        $slLine = ("$slRaw" -split "`r")[-1].Trim()
        if (-not $slLine) { continue }
        $slLast = $slLine
        if (-not $slPick -and $slLine -match '(^|\s)(fatal|error): ') { $slPick = $slLine }
    }
    if (-not $slPick) { $slPick = $slLast }
    if ($slPick -and $slPick.Length -gt 240) { $slPick = $slPick.Substring(0, 240) }
    return $slPick
}

function Get-IndusNativeDetail {
    if ($script:IndusNativeError) { return ": $($script:IndusNativeError)" }
    return ''
}

# Runs a native block with one status line; its output goes to <home>\logs\install.log, and to the
# console too with -Verbose, in CI, when stdout is redirected (the desktop app reads it) or when the
# log cannot be opened. $LASTEXITCODE stays the caller's.
function Invoke-IndusLogged {
    param([string]$LoggedLabel, [scriptblock]$LoggedBlock, [switch]$MayFail)
    $script:IndusNativeError = ''
    Write-IndusInfo $LoggedLabel
    $lgWriter = $null
    try {
        $lgLog = Get-IndusLogPath
        $lgDir = Split-Path -Parent $lgLog
        if (-not (Test-Path -LiteralPath $lgDir)) { New-Item -ItemType Directory -Force -Path $lgDir | Out-Null }
        $lgWriter = New-Object System.IO.StreamWriter($lgLog, $true, (New-Object System.Text.UTF8Encoding($false)))
        $lgWriter.WriteLine("==> $LoggedLabel ($(Get-IndusUtcStamp))")
    } catch {
        $lgWriter = $null
    }
    $lgStream = [bool]$script:IndusOpts.Verbose -or [bool]$env:CI -or -not $lgWriter
    if (-not $lgStream) {
        try { $lgStream = [Console]::IsOutputRedirected } catch { $lgStream = $true }
    }
    $lgTail = New-Object 'System.Collections.Generic.List[string]'
    $lgCode = 0
    try {
        $global:LASTEXITCODE = 0
        Invoke-IndusNative { & $LoggedBlock 2>&1 } | ForEach-Object {
            $lgLine = "$_".TrimEnd("`r")
            if ($lgWriter) { $lgWriter.WriteLine($lgLine) }
            if ($lgStream) { Write-Host "    $lgLine" }
            $lgTail.Add($lgLine)
            if ($lgTail.Count -gt 20) { $lgTail.RemoveAt(0) }
        }
        $lgCode = $global:LASTEXITCODE
    } finally {
        if ($lgWriter) { $lgWriter.Dispose() }
    }
    if ($lgCode) {
        $script:IndusNativeError = Select-IndusErrorLine $lgTail
        if (-not $MayFail) {
            Write-IndusWarn "$LoggedLabel failed (exit $lgCode). Last output:"
            foreach ($lgShown in $lgTail) { Write-Host "    $lgShown" }
            Write-Host "    full log: $(Get-IndusLogPath)"
        }
    }
    $global:LASTEXITCODE = $lgCode
}
