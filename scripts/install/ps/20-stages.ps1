
# ---------------------------------------------------------------------------------------------
# Stages. Same names, order and meaning as install.sh (the shared table is stages.json).
# ---------------------------------------------------------------------------------------------

$script:IndusStableTag = '^v[0-9]{1,3}\.[0-9]+\.[0-9]+$'
$script:IndusCanaryTag = '^v[0-9]{1,3}\.[0-9]+\.[0-9]+-canary\.[0-9]{8}T[0-9]{6}Z$'
$script:IndusBinWasOnPath = $null
$script:IndusPython = $null

function Get-IndusBranch {
    if ($script:IndusOpts.Branch) { return [string]$script:IndusOpts.Branch }
    return 'main'
}

function Get-IndusVenvIndus {
    return (Join-Path (Join-Path (Join-Path $script:IndusPaths.InstallDir '.venv') 'Scripts') 'indus.exe')
}

function Test-IndusEmptyDir([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { return $false }
    if ($item.LinkType) { return $false }
    return -not (Get-ChildItem -LiteralPath $Path -Force | Select-Object -First 1)
}

# -- prerequisites ------------------------------------------------------------------------------

function Invoke-IndusPrerequisites {
    if (-not (Test-IndusWindows)) {
        Stop-IndusStage 'install.ps1 installs on Windows; on Linux and macOS run install.sh' 'unsupported_platform'
    }
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Stop-IndusStage 'Windows PowerShell 5.1 or PowerShell 7 is needed' 'unsupported_platform'
    }
    Write-IndusLongPathAdvice
    Use-IndusGit | Out-Null
    Write-IndusOk 'prerequisites ready (pinned Git)'
}

# -- channels -----------------------------------------------------------------------------------

function ConvertFrom-IndusYamlScalar([string]$Text) {
    $value = ($Text -replace '\s+#.*$', '').Trim()
    if ($value.Length -ge 2 -and ($value[0] -eq '"' -or $value[0] -eq "'") -and $value[-1] -eq $value[0]) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

# The install id the updater keys its records by: the first 16 hex digits of the SHA-256 of the
# checkout's full path.
function Get-IndusInstallId([string]$CodeDir) {
    $full = (Get-Item -LiteralPath $CodeDir -Force).FullName.TrimEnd('\', '/')
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($full))
    } finally {
        $hasher.Dispose()
    }
    return (-join ($digest | ForEach-Object { $_.ToString('x2') })).Substring(0, 16)
}

# updates.installs.<id>.<Name> from the home's config.yaml (the schema key `indus update` reads),
# or ''. A small reader of the block YAML the settings writer produces: a scalar comes back as it
# is, a list (block '- item' lines or '[a, b]') as its items joined by single spaces. Anything it
# cannot read counts as not set.
function Get-IndusInstallSetting([string]$CodeDir, [string]$Name) {
    $config = Join-Path $script:IndusPaths.Home 'config.yaml'
    if (-not (Test-Path -LiteralPath $config)) { return '' }
    try {
        $id = Get-IndusInstallId $CodeDir
        $lines = [IO.File]::ReadAllLines($config)
    } catch {
        return ''
    }
    $stack = New-Object 'System.Collections.Generic.List[object]'
    $items = New-Object 'System.Collections.Generic.List[string]'
    $collecting = $false
    $keyIndent = 0
    foreach ($line in $lines) {
        if ($line -match '^\s*(#|$)') { continue }
        if ($collecting) {
            if ($line -match '^( *)- (.*)$' -and $Matches[1].Length -ge $keyIndent) {
                $item = ConvertFrom-IndusYamlScalar $Matches[2]
                if ($item) { $items.Add($item) }
                continue
            }
            return ($items -join ' ')
        }
        if ($line -notmatch '^( *)([^:#]+):(.*)$') { continue }
        $indent = $Matches[1].Length
        $key = ConvertFrom-IndusYamlScalar $Matches[2]
        $value = ConvertFrom-IndusYamlScalar $Matches[3]
        while ($stack.Count -gt 0 -and $stack[$stack.Count - 1].Indent -ge $indent) { $stack.RemoveAt($stack.Count - 1) }
        $stack.Add([pscustomobject]@{ Indent = $indent; Key = $key })
        if ($stack.Count -eq 4 -and $stack[0].Key -ceq 'updates' -and $stack[1].Key -ceq 'installs' -and
            $stack[2].Key -ceq $id -and $key -ceq $Name) {
            if ($value -eq '') {
                $collecting = $true
                $keyIndent = $indent
                continue
            }
            if ($value.StartsWith('[') -and $value.EndsWith(']')) {
                $parts = @($value.Substring(1, $value.Length - 2) -split ',' |
                    ForEach-Object { ConvertFrom-IndusYamlScalar $_ } | Where-Object { $_ })
                return ($parts -join ' ')
            }
            return $value
        }
    }
    if ($collecting) { return ($items -join ' ') }
    return ''
}

# The channel saved for this checkout (updates.installs.<id>.channel), or ''.
function Get-IndusSavedChannel([string]$CodeDir) {
    $value = Get-IndusInstallSetting $CodeDir 'channel'
    if ($script:IndusChannels -ccontains $value) { return $value }
    return ''
}

function Test-IndusOfficialOrigin([string]$Origin) {
    $official = (Get-IndusRepository) -replace '\.git$', ''
    $given = ($Origin.Trim().TrimEnd('/')) -replace '\.git$', ''
    return [string]::Equals($given, $official, [StringComparison]::OrdinalIgnoreCase)
}

# Stable for the official repository's main line; main for forks, other branches and pins.
function Get-IndusDefaultChannel([string]$Origin) {
    if ($script:IndusOpts.Commit) { return 'main' }
    if ((Get-IndusBranch) -ne 'main') { return 'main' }
    if (Test-IndusOfficialOrigin $Origin) { return 'stable' }
    return 'main'
}

# -Channel, else the checkout's saved choice (an existing checkout keeps it), else the default.
function Get-IndusEffectiveChannel([string]$CodeDir, [string]$Origin) {
    if ($script:IndusOpts.Channel) { return [string]$script:IndusOpts.Channel }
    if (-not $script:IndusOpts.Commit -and (Test-Path -LiteralPath (Join-Path $CodeDir '.git'))) {
        $saved = Get-IndusSavedChannel $CodeDir
        if ($saved) { return $saved }
    }
    return (Get-IndusDefaultChannel $Origin)
}

# A sortable key for vX.Y.Z and vX.Y.Z-canary.<stamp>: a canary sorts below its own release.
function Get-IndusTagKey([string]$Tag) {
    $core = $Tag.Substring(1)
    $stamp = ''
    $release = '1'
    $dash = $core.IndexOf('-')
    if ($dash -ge 0) {
        $stamp = $core.Substring($dash + 1)
        $core = $core.Substring(0, $dash)
        $release = '0'
    }
    $parts = $core.Split('.')
    return ('{0:D6}.{1:D9}.{2:D9}.{3}.{4}' -f [int]$parts[0], [long]$parts[1], [long]$parts[2], $release, $stamp)
}

function Get-IndusRemoteTags([string]$Origin) {
    $tags = @()
    foreach ($line in @(Invoke-IndusNative { git ls-remote --tags --refs $Origin 'refs/tags/v*' 2>$null })) {
        $name = ("$line" -replace '^.*refs/tags/', '').Trim()
        if ($name) { $tags += $name }
    }
    return $tags
}

function Select-IndusNewestTag([string[]]$Tags) {
    $best = ''
    $bestKey = ''
    foreach ($tag in $Tags) {
        $tagKey = Get-IndusTagKey $tag
        if (-not $best -or [string]::CompareOrdinal($tagKey, $bestKey) -gt 0) {
            $best = $tag
            $bestKey = $tagKey
        }
    }
    return $best
}

# The newest published vX.Y.Z: the GitHub API, then the releases/latest redirect, then the
# remote's tags. '' when none answers.
function Get-IndusLatestStable([string]$Origin) {
    $tag = ''
    if (Test-IndusOfficialOrigin $Origin) {
        $repository = Get-IndusRepository
        $slug = $repository -replace '^https://github\.com/', ''
        try {
            $answer = Invoke-RestMethod -Uri "https://api.github.com/repos/$slug/releases/latest" -TimeoutSec 20 `
                -Headers @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'indus-installer' }
            $tag = [string]$answer.tag_name
        } catch {
            $tag = ''
        }
        if ($tag -cnotmatch $script:IndusStableTag) {
            $tag = ''
            try {
                $request = [Net.HttpWebRequest]::Create("$repository/releases/latest")
                $request.Method = 'HEAD'
                $request.AllowAutoRedirect = $false
                $request.Timeout = 20000
                $request.UserAgent = 'indus-installer'
                $response = $request.GetResponse()
                try {
                    if ([string]$response.Headers['Location'] -match '/releases/tag/([^/]+)$') { $tag = $Matches[1] }
                } finally {
                    $response.Close()
                }
            } catch {
                $tag = ''
            }
        }
    }
    if ($tag -cnotmatch $script:IndusStableTag) {
        $tag = Select-IndusNewestTag @(Get-IndusRemoteTags $Origin | Where-Object { $_ -cmatch $script:IndusStableTag })
    }
    if ($tag -cmatch $script:IndusStableTag) { return $tag }
    return ''
}

# The newest canary, or a newer stable when one exists (canary users are never stranded).
function Get-IndusLatestCanary([string]$Origin) {
    $tags = @(Get-IndusRemoteTags $Origin | Where-Object { $_ -cmatch $script:IndusCanaryTag -or $_ -cmatch $script:IndusStableTag })
    return (Select-IndusNewestTag $tags)
}

# '' = follow the branch; a tag = install that release; '-' = a release applies but none answered.
function Resolve-IndusRelease([string]$Origin, [string]$ChosenChannel) {
    if ($script:IndusOpts.Commit -or $ChosenChannel -eq 'main') { return '' }
    if ($ChosenChannel -eq 'canary') {
        $tag = Get-IndusLatestCanary $Origin
    } else {
        $tag = Get-IndusLatestStable $Origin
    }
    if ($tag) { return $tag }
    return '-'
}

# -- repository ---------------------------------------------------------------------------------

function Invoke-IndusRepository {
    $codeDir = $script:IndusPaths.InstallDir
    $gitDir = Join-Path $codeDir '.git'
    if ((Test-Path -LiteralPath $codeDir) -and -not (Test-Path -LiteralPath $gitDir)) {
        if (-not (Test-IndusEmptyDir $codeDir)) {
            Stop-IndusStage "$codeDir exists and is not an Indus Agent checkout; move it aside, or install elsewhere with -InstallDir <path>" 'dir_not_checkout'
        }
    }
    Use-IndusGit | Out-Null
    if (Test-Path -LiteralPath $gitDir) {
        Invoke-IndusNative { git -C $codeDir rev-parse --verify HEAD 2>$null } | Out-Null
        if ($LASTEXITCODE) {
            $aside = $codeDir + '.broken-' + (Get-IndusCompactStamp)
            Write-IndusWarn "$codeDir has no commits (an interrupted clone); moving it aside to $aside"
            Move-Item -LiteralPath $codeDir -Destination $aside
        }
    }
    if (Test-Path -LiteralPath $gitDir) {
        Update-IndusCheckout $codeDir
    } else {
        New-IndusCheckout $codeDir
    }
}

function Receive-IndusTag([string]$Tree, [string]$Tag) {
    Invoke-IndusLogged "Fetching $Tag" { git -C $Tree fetch origin "+refs/tags/${Tag}:refs/tags/${Tag}" } -MayFail
    return (-not $LASTEXITCODE)
}

# A fresh install: clone into a sibling staging dir and move it into place only when the files
# are checked out, so an interrupted clone never leaves a half checkout behind.
function New-IndusCheckout([string]$CodeDir) {
    if (Test-Path -LiteralPath $CodeDir) { Remove-Item -LiteralPath $CodeDir -Force }
    $parentDir = Split-Path -Parent $CodeDir
    New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
    $url = Get-IndusCloneUrl
    $branch = Get-IndusBranch
    $pin = [string]$script:IndusOpts.Commit
    $chosen = Get-IndusEffectiveChannel $CodeDir $url
    $release = Resolve-IndusRelease $url $chosen
    if ($release -eq '-') {
        Stop-IndusStage "could not look up the latest $chosen release; rerun to retry, or pass -Channel main to install the branch tip" 'git_fetch_failed'
    }
    if ($release) { Write-IndusInfo "Installing $release ($chosen channel)" }
    $staging = Join-Path $parentDir ('.indus-clone-' + $PID + '-' + (Get-Random))
    $tree = Join-Path $staging 'tree'
    $noCheckout = @()
    if ($pin -or $release) { $noCheckout = @('--no-checkout') }
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    try {
        $cloned = $false
        $deferred = $false
        foreach ($attempt in 1..3) {
            Invoke-IndusLogged "Cloning $url ($branch), attempt $attempt of 3" {
                git clone --config core.longpaths=true --filter=blob:none @noCheckout --branch $branch $url $tree
            } -MayFail
            if (-not $LASTEXITCODE) { $cloned = $true; break }
            Remove-Item -LiteralPath $tree -Recurse -Force -ErrorAction SilentlyContinue
            if ($attempt -lt 3) { & $script:IndusSleep ($attempt * 5000) | Out-Null }
        }
        if (-not $cloned) {
            Write-IndusWarn 'the clone failed three times; trying once more without checking out files'
            Invoke-IndusLogged 'Cloning the history only' {
                git clone --config core.longpaths=true --filter=blob:none --no-checkout --branch $branch $url $tree
            } -MayFail
            if (-not $LASTEXITCODE) { $cloned = $true; $deferred = $true }
        }
        if (-not $cloned) { Stop-IndusStage "git clone failed; nothing was installed$(Get-IndusNativeDetail)" 'git_clone_failed' }
        if ($pin) {
            Invoke-IndusNative { git -C $tree merge-base --is-ancestor $pin "origin/$branch" 2>$null } | Out-Null
            if ($LASTEXITCODE) { Stop-IndusStage "commit $pin is not on branch $branch" 'commit_not_on_branch' }
        }
        if ($release -and -not (Receive-IndusTag $tree $release)) {
            Stop-IndusStage "could not fetch $release$(Get-IndusNativeDetail)" 'git_fetch_failed'
        }
        if ($pin -or $release -or $deferred) {
            $detachAt = $pin
            if (-not $detachAt) { $detachAt = $release }
            $materialised = $false
            foreach ($attempt in 1..2) {
                if ($detachAt) {
                    Invoke-IndusLogged "Checking out $detachAt" { git -C $tree checkout --force --detach $detachAt } -MayFail
                } else {
                    Invoke-IndusLogged 'Checking out the files' { git -C $tree reset --hard HEAD } -MayFail
                }
                if (-not $LASTEXITCODE) { $materialised = $true; break }
                if ($attempt -lt 2) { & $script:IndusSleep 5000 | Out-Null }
            }
            if (-not $materialised) {
                Stop-IndusStage "could not check out the files; nothing was installed$(Get-IndusNativeDetail)" 'git_checkout_failed'
            }
        }
        Move-Item -LiteralPath $tree -Destination $CodeDir
        Write-IndusOk "Indus Agent cloned into $CodeDir"
    } finally {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Parks local work before anything moves: unmerged index entries are cleared first (stash refuses
# them), then everything goes into a stash with a findable label.
function Save-IndusLocalChanges([string]$CodeDir, [string]$Stamp) {
    $dirty = Invoke-IndusNative { git -C $CodeDir status --porcelain 2>$null }
    if (-not $dirty) { return }
    $unmerged = Invoke-IndusNative { git -C $CodeDir ls-files --unmerged 2>$null }
    if ($unmerged) {
        Write-IndusWarn 'clearing unmerged index entries left by an earlier conflict'
        Invoke-IndusNative { git -C $CodeDir reset -q 2>&1 } | Out-Null
        if ($LASTEXITCODE) { Stop-IndusStage "could not clear the unmerged index in $CodeDir" 'local_changes_blocked' }
    }
    $label = 'indus-install-autostash-' + $Stamp
    Invoke-IndusLogged 'Stashing local changes' { git -C $CodeDir stash push --include-untracked -m $label } -MayFail
    if ($LASTEXITCODE) {
        Stop-IndusStage "could not stash the local changes in $CodeDir; commit or move them, then rerun" 'local_changes_blocked'
    }
    Write-IndusWarn "local changes stashed as $label (see: git stash list)"
}

# An existing checkout: a release channel only moves forward onto its newest release (any other
# move is `indus update`'s); main fast-forwards, and a diverged history is reset only after its
# commits are saved under refs/indus/install-backups/<stamp>.
function Update-IndusCheckout([string]$CodeDir) {
    Invoke-IndusNative { git -C $CodeDir config core.longpaths true 2>&1 } | Out-Null
    if ($env:INDUS_REPO_URL) {
        $cloneUrl = Get-IndusCloneUrl
        Invoke-IndusNative { git -C $CodeDir remote set-url origin $cloneUrl 2>&1 } | Out-Null
        if ($LASTEXITCODE) { Stop-IndusStage "could not point origin at $cloneUrl" 'git_fetch_failed' }
    }
    $origin = ([string](Invoke-IndusNative { git -C $CodeDir config --get remote.origin.url 2>$null })).Trim()
    $asked = [string]$script:IndusOpts.Channel
    $chosen = Get-IndusEffectiveChannel $CodeDir $origin
    $branch = Get-IndusBranch
    $pin = [string]$script:IndusOpts.Commit
    $stamp = Get-IndusCompactStamp
    $release = Resolve-IndusRelease $origin $chosen
    if ($release -eq '-') {
        if (-not $asked) {
            Write-IndusWarn "could not look up the latest $chosen release; keeping the checkout where it is"
            return
        }
        Stop-IndusStage "could not look up the latest $chosen release; rerun to retry, or pass -Channel main" 'git_fetch_failed'
    }
    if ($release) {
        if (-not (Receive-IndusTag $CodeDir $release)) {
            if (-not $asked) { Write-IndusWarn "could not fetch $release; keeping the checkout where it is"; return }
            Stop-IndusStage "could not fetch $release$(Get-IndusNativeDetail)" 'git_fetch_failed'
        }
        Invoke-IndusNative { git -C $CodeDir merge-base --is-ancestor $release HEAD 2>$null } | Out-Null
        if (-not $LASTEXITCODE) {
            Write-IndusInfo "Keeping the checkout: it already has $release (indus update moves it on)"
            return
        }
        Invoke-IndusNative { git -C $CodeDir merge-base --is-ancestor HEAD $release 2>$null } | Out-Null
        if ($LASTEXITCODE) {
            Write-IndusInfo "Keeping the checkout: $release does not descend from it (indus update decides that move)"
            return
        }
        Save-IndusLocalChanges $CodeDir $stamp
        Invoke-IndusLogged "Checking out $release" { git -C $CodeDir checkout --detach $release }
        if ($LASTEXITCODE) { Stop-IndusStage "git checkout failed$(Get-IndusNativeDetail)" 'git_checkout_failed' }
        return
    }
    Invoke-IndusLogged "Fetching origin/$branch" { git -C $CodeDir fetch origin "+refs/heads/${branch}:refs/remotes/origin/${branch}" }
    if ($LASTEXITCODE) { Stop-IndusStage "git fetch failed$(Get-IndusNativeDetail)" 'git_fetch_failed' }
    Save-IndusLocalChanges $CodeDir $stamp
    Invoke-IndusNative { git -C $CodeDir show-ref --verify --quiet "refs/heads/$branch" 2>$null } | Out-Null
    if ($LASTEXITCODE) {
        Invoke-IndusLogged "Checking out $branch" { git -C $CodeDir checkout -b $branch "origin/$branch" }
    } else {
        Invoke-IndusLogged "Checking out $branch" { git -C $CodeDir checkout $branch }
    }
    if ($LASTEXITCODE) { Stop-IndusStage "git checkout failed$(Get-IndusNativeDetail)" 'git_checkout_failed' }
    Invoke-IndusLogged "Fast-forwarding to origin/$branch" { git -C $CodeDir merge --ff-only --no-stat "origin/$branch" } -MayFail
    if ($LASTEXITCODE) {
        $ahead = ([string](Invoke-IndusNative { git -C $CodeDir rev-list --count "origin/$branch..HEAD" 2>$null })).Trim()
        if ($LASTEXITCODE -or $ahead -notmatch '^[0-9]+$') { Stop-IndusStage 'could not count the local commits before the reset' 'git_reset_failed' }
        if ([int]$ahead -gt 0) {
            $backup = 'refs/indus/install-backups/' + $stamp
            Invoke-IndusNative { git -C $CodeDir update-ref $backup HEAD 2>&1 } | Out-Null
            if ($LASTEXITCODE) { Stop-IndusStage "could not save $ahead local commit(s); refusing to reset" 'git_reset_failed' }
            Write-IndusWarn "$ahead commit(s) not on origin/$branch saved as $backup"
        }
        Invoke-IndusLogged "Resetting to origin/$branch" { git -C $CodeDir reset --hard "origin/$branch" }
        if ($LASTEXITCODE) { Stop-IndusStage "git reset failed$(Get-IndusNativeDetail)" 'git_reset_failed' }
        Write-IndusWarn "the checkout could not fast-forward; it now matches origin/$branch"
    }
    if ($pin) {
        Invoke-IndusNative { git -C $CodeDir merge-base --is-ancestor $pin "origin/$branch" 2>$null } | Out-Null
        if ($LASTEXITCODE) { Stop-IndusStage "commit $pin is not on branch $branch" 'commit_not_on_branch' }
        Invoke-IndusLogged "Pinning $pin" { git -C $CodeDir checkout --detach $pin }
        if ($LASTEXITCODE) { Stop-IndusStage "could not check out $pin$(Get-IndusNativeDetail)" 'git_checkout_failed' }
    }
}

# -- runtime and dependencies -------------------------------------------------------------------

# Python from uv: an existing 3.13/3.14 is accepted, else the pinned minor is downloaded.
function Get-IndusPython {
    if ($script:IndusPython) { return $script:IndusPython }
    $uv = Get-IndusUv
    $pins = Get-IndusPins
    $minor = '3.14'
    $floor = '3.13'
    if ($pins.python -and $pins.python.provision) { $minor = [string]$pins.python.provision }
    if ($pins.python -and $pins.python.minor) { $minor = [string]$pins.python.minor }
    if ($pins.python -and $pins.python.floor) { $floor = [string]$pins.python.floor }
    $minorParts = $minor.Split('.')
    $ceiling = $minorParts[0] + '.' + ([int]$minorParts[1] + 1)
    $wanted = ">=$floor,<$ceiling"
    $found = (Invoke-IndusNative -Utf8 { & $uv python find --no-project $wanted 2>$null }) -join ''
    if ($LASTEXITCODE -or -not $found) {
        $request = $minor
        if ((Get-IndusArch) -eq 'arm64') { $request = "cpython-$minor-windows-aarch64-none" }
        Invoke-IndusLogged "Downloading Python $minor" { & $uv python install $request }
        if ($LASTEXITCODE) { Stop-IndusStage "Python $minor could not be installed$(Get-IndusNativeDetail)" 'python_install_failed' }
        $found = (Invoke-IndusNative -Utf8 { & $uv python find --no-project $request 2>$null }) -join ''
    }
    if ($LASTEXITCODE -or -not $found) { Stop-IndusStage 'uv found no usable Python' 'python_install_failed' }
    $script:IndusPython = $found.Trim()
    return $script:IndusPython
}

function Invoke-IndusRuntime {
    $uv = Get-IndusUv
    $python = Get-IndusPython
    Write-IndusOk "uv ($uv) and Python ($python) ready"
}

# True when an optional group of extras stays out of this install: this run's skip flag says so,
# or an earlier install saved its extras (updates.installs.<id>.extras) without them.
function Test-IndusLeftOut([bool]$Flag, $Group, [string[]]$Saved) {
    $names = @($Group | Where-Object { $_ })
    if ($names.Count -eq 0) { return $Flag }
    if ($Flag) { return $true }
    if ($Saved.Count -eq 0) { return $false }
    foreach ($name in $names) {
        if (-not ($Saved -ccontains $name)) { return $true }
    }
    return $false
}

# What this install leaves out: this run's skip flags plus what the saved extras lack.
function Get-IndusChoices {
    $saved = ''
    if (Test-Path -LiteralPath (Join-Path $script:IndusPaths.InstallDir '.git')) {
        $saved = Get-IndusInstallSetting $script:IndusPaths.InstallDir 'extras'
    }
    $words = @($saved -split '\s+' | Where-Object { $_ })
    $table = $script:IndusStages.extras
    return @{
        skip_browser      = Test-IndusLeftOut ([bool]$script:IndusOpts.SkipBrowser) $table.unless_skip_browser $words
        skip_computer_use = Test-IndusLeftOut ([bool]$script:IndusOpts.SkipComputerUse) $table.unless_skip_computer_use $words
    }
}

function Get-IndusSkipWords($Choices) {
    $words = @()
    if ($Choices.skip_browser) { $words += 'browser' }
    if ($Choices.skip_computer_use) { $words += 'computer-use' }
    return ($words -join ',')
}

function Get-IndusExtras($Choices) {
    $table = $script:IndusStages.extras
    $extras = @($table.always)
    if (-not $Choices.skip_browser) { $extras += @($table.unless_skip_browser) }
    if (-not $Choices.skip_computer_use) { $extras += @($table.unless_skip_computer_use) }
    return @($extras | Where-Object { $_ })
}

function Get-IndusVenvPython {
    return (Join-Path (Join-Path (Join-Path $script:IndusPaths.InstallDir '.venv') 'Scripts') 'python.exe')
}

function Invoke-IndusDependencies {
    if (-not (Test-Path -LiteralPath (Join-Path $script:IndusPaths.InstallDir 'uv.lock'))) {
        Stop-IndusStage "$($script:IndusPaths.InstallDir) has no uv.lock; run the repository stage first" 'deps_install_failed'
    }
    $uv = Get-IndusUv
    $python = Get-IndusPython
    $choices = Get-IndusChoices
    $syncArgs = @('sync', '--frozen', '--no-dev', '--python', $python)
    foreach ($extra in (Get-IndusExtras $choices)) { $syncArgs += @('--extra', $extra) }
    Push-Location -LiteralPath $script:IndusPaths.InstallDir
    try {
        Invoke-IndusLogged 'Installing Python dependencies (locked)' { & $uv @syncArgs }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code) { Stop-IndusStage "dependency install failed$(Get-IndusNativeDetail)" 'deps_install_failed' }
    if (-not (Test-Path -LiteralPath (Get-IndusVenvIndus))) {
        Stop-IndusStage 'the indus command is missing after the install' 'deps_install_failed'
    }
    $skipped = Get-IndusSkipWords $choices
    if ($skipped) { Write-IndusInfo "left out: $skipped" }
    Write-IndusOk 'dependencies installed'
}

# -- config -------------------------------------------------------------------------------------

# The installed package makes the home skeleton and seeds config.yaml itself (the same writer
# `indus config edit` uses), so the installer never duplicates the layout.
$script:IndusSeedPy = @'
import sys
from pathlib import Path

from indus.cli.commands.config_read import seed_config
from indus.foundation.layout import ensure_home
from indus.foundation.scope import make_scope

scope = make_scope(Path(sys.argv[1]))
ensure_home(scope)
seed_config(scope)
'@

function Invoke-IndusConfig {
    $homeDir = $script:IndusPaths.Home
    $python = Get-IndusVenvPython
    if (-not (Test-Path -LiteralPath $python)) {
        Stop-IndusStage 'the Python environment is missing; run the dependencies stage first' 'deps_install_failed'
    }
    $seed = $script:IndusSeedPy
    Invoke-IndusLogged "Preparing $homeDir" { & $python -I -c $seed $homeDir }
    if ($LASTEXITCODE) { Stop-IndusStage "could not prepare $homeDir$(Get-IndusNativeDetail)" 'config_failed' }
    $envFile = Join-Path $homeDir '.env'
    try {
        if (-not (Test-Path -LiteralPath $envFile)) { New-Item -ItemType File -Path $envFile | Out-Null }
    } catch {
        Stop-IndusStage "could not create $envFile" 'config_failed'
    }
    # The extras this install synced, so `indus update` and later installs keep the same set.
    $extras = @(Get-IndusExtras (Get-IndusChoices))
    $listed = '[' + ($extras -join ', ') + ']'
    Save-IndusInstallSetting 'extras' $listed "could not save the extras ($($extras -join ' ')); later updates may install a different set"
    Write-IndusOk "settings ready in $homeDir"
}

# -- products -----------------------------------------------------------------------------------

function Add-IndusUserPath([string]$Dir) {
    $key = $null
    try {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    } catch {
        $key = $null
    }
    if (-not $key) {
        Write-IndusWarn "could not open the user environment; add $Dir to PATH yourself"
    } else {
        try {
            $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $parts = @($raw -split ';' | Where-Object { $_ })
            $present = $false
            foreach ($part in $parts) {
                if (Test-IndusSamePath ([Environment]::ExpandEnvironmentVariables($part)) $Dir) { $present = $true }
            }
            if (-not $present) {
                $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
                try { $kind = $key.GetValueKind('Path') } catch { $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
                $key.SetValue('Path', ((@($Dir) + $parts) -join ';'), $kind)
                # Unsetting an unused name announces the change to windows started from now on.
                [Environment]::SetEnvironmentVariable('INDUS_PATH_REFRESH', $null, 'User')
                Write-IndusOk "added $Dir to your user PATH (new terminals pick it up)"
            }
        } finally {
            $key.Close()
        }
    }
    $onPath = $false
    foreach ($item in @($env:Path -split ';' | Where-Object { $_ })) { if (Test-IndusSamePath $item $Dir) { $onPath = $true } }
    if ($null -eq $script:IndusBinWasOnPath) { $script:IndusBinWasOnPath = $onPath }
    if (-not $onPath) { $env:Path = $Dir + ';' + $env:Path }
}

# <home>\bin\indus.cmd runs the environment's indus.exe; the uninstaller recognises it by the path
# it names.
function Publish-IndusLauncher {
    $target = Get-IndusVenvIndus
    if (-not (Test-Path -LiteralPath $target)) {
        Stop-IndusStage "the environment has no indus command ($target); run the dependencies stage first" 'products_build_failed'
    }
    $binDir = Join-Path $script:IndusPaths.Home 'bin'
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $launcher = Join-Path $binDir 'indus.cmd'
    $text = "@echo off`r`nrem indus launcher written by install.ps1 for $target`r`n`"$target`" %*`r`n"
    $encoding = [Text.Encoding]::ASCII
    try { $encoding = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage) } catch { $encoding = [Text.Encoding]::ASCII }
    [IO.File]::WriteAllText($launcher, $text, $encoding)
    Add-IndusUserPath $binDir
    Write-IndusOk "indus command: $launcher"
}

# A checkout serves its own built pages (apps\web\dist) when a developer built them; otherwise a
# release install gets the pages its release was built with (F-3), as install.sh does: the
# environment's python runs `-m indus.web.bundle fetch`, which checks them against the manifest
# digest the release tag records and publishes them into src\indus\web\static. Exit 3 means HEAD
# is on no release (a main install builds its own). Never fatal: the dashboard API still works.
function Install-IndusWebPages {
    $pgDir = $script:IndusPaths.InstallDir
    $pgBuilt = Join-Path (Join-Path (Join-Path (Join-Path $pgDir 'apps') 'web') 'dist') 'index.html'
    if (Test-Path -LiteralPath $pgBuilt -PathType Leaf) {
        Write-IndusInfo 'the dashboard pages are built in this checkout (apps\web\dist)'
        return
    }
    $pgPython = Get-IndusVenvPython
    if (-not (Test-Path -LiteralPath $pgPython)) {
        Write-IndusWarn "the dashboard pages were not fetched ($pgPython is missing); rerun the installer"
        return
    }
    $pgTarget = Join-Path (Join-Path (Join-Path (Join-Path $pgDir 'src') 'indus') 'web') 'static'
    Use-IndusGit | Out-Null
    Invoke-IndusLogged 'Fetching the dashboard pages' { & $pgPython -m indus.web.bundle fetch --repo $pgDir --target $pgTarget } -MayFail
    $pgCode = $LASTEXITCODE
    $global:LASTEXITCODE = 0
    if ($pgCode -eq 0) {
        Write-IndusOk 'the dashboard pages of this release are in place'
    } elseif ($pgCode -eq 3) {
        Write-IndusInfo 'This checkout is not on a release, so it has no prebuilt dashboard pages; build them with: pnpm install && pnpm --filter @indus/web build'
    } else {
        Write-IndusWarn "the dashboard pages could not be fetched$(Get-IndusNativeDetail); the dashboard serves only its API until they are (rerun the installer, or build them with pnpm --filter @indus/web build)"
    }
}

function Invoke-IndusProducts {
    Publish-IndusLauncher
    Install-IndusWebPages
    if ($script:IndusOpts.IncludeDesktop) { Invoke-IndusDesktop }
}

# -- setup and gateway --------------------------------------------------------------------------

function Invoke-IndusSetup {
    if (-not (Test-IndusConsole)) {
        Write-IndusInfo 'No console to answer questions; run `indus setup` later.'
        return
    }
    $indusExe = Get-IndusVenvIndus
    Invoke-IndusNative { & $indusExe setup }
    if ($LASTEXITCODE) { Stop-IndusStage "indus setup failed (exit $LASTEXITCODE)" 'setup_failed' }
}

function Write-IndusWslNote {
    Write-IndusInfo 'On Windows this install runs the desktop app. The indus CLI, TUI and the messaging gateway are'
    Write-IndusInfo 'supported through WSL2: open a WSL terminal and run the install.sh command there.'
}

# Native Windows has no gateway service in 1.0; the gateway runs under WSL2.
function Invoke-IndusGateway {
    Write-IndusInfo 'The messaging gateway runs under WSL2 on Windows; nothing to set up here.'
}

# -- complete -----------------------------------------------------------------------------------

# updates.installs.<id>.<Name> = Value through `indus config set` (the key `indus update` reads);
# a failure only warns.
function Save-IndusInstallSetting([string]$Name, [string]$Value, [string]$Warning) {
    $id = Get-IndusInstallId $script:IndusPaths.InstallDir
    $indusExe = Get-IndusVenvIndus
    Invoke-IndusLogged "Saving updates.installs.$id.$Name" { & $indusExe config set --force "updates.installs.$id.$Name" $Value } -MayFail
    if ($LASTEXITCODE) { Write-IndusWarn $Warning }
}

function Invoke-IndusComplete {
    Use-IndusGit | Out-Null
    $codeDir = $script:IndusPaths.InstallDir
    $head = ([string](Invoke-IndusNative { git -C $codeDir rev-parse HEAD 2>$null })).Trim()
    if ($LASTEXITCODE -or -not $head) { Stop-IndusStage "could not read the installed commit in $codeDir" 'other' }
    $origin = ([string](Invoke-IndusNative { git -C $codeDir config --get remote.origin.url 2>$null })).Trim()
    # The release HEAD is on (newest when several), for `indus --version` (F-4); '' on main.
    $headTags = @(Invoke-IndusNative { git -C $codeDir tag --points-at HEAD --list 'v*' 2>$null } |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_ -cmatch $script:IndusStableTag -or $_ -cmatch $script:IndusCanaryTag })
    $releaseTag = [string](Select-IndusNewestTag $headTags)
    $global:LASTEXITCODE = 0
    if ($releaseTag) { $releaseTag = $releaseTag.Substring(1) }
    $record = [ordered]@{
        schema       = 1
        commit       = $head
        branch       = Get-IndusBranch
        channel      = Get-IndusEffectiveChannel $codeDir $origin
        version      = $releaseTag
        completed_at = Get-IndusUtcStamp
    }
    $marker = Join-Path $codeDir '.indus-bootstrap.json'
    $tmpFile = $marker + '.tmp'
    [IO.File]::WriteAllText($tmpFile, ($record | ConvertTo-Json -Compress), (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmpFile -Destination $marker -Force
    if ($script:IndusOpts.Channel) {
        $chosen = $script:IndusOpts.Channel
        Save-IndusInstallSetting 'channel' $chosen "could not save the $chosen channel; 'indus update --set-channel $chosen' does it"
    }
    Write-IndusOk "Indus Agent installed at $codeDir ($head)"
    Write-IndusWslNote
}

function Invoke-IndusStage([string]$Name) {
    switch -CaseSensitive ($Name) {
        'prerequisites' { Invoke-IndusPrerequisites }
        'repository' { Invoke-IndusRepository }
        'runtime' { Invoke-IndusRuntime }
        'dependencies' { Invoke-IndusDependencies }
        'config' { Invoke-IndusConfig }
        'products' { Invoke-IndusProducts }
        'setup' { Invoke-IndusSetup }
        'gateway' { Invoke-IndusGateway }
        'complete' { Invoke-IndusComplete }
        'desktop' { Invoke-IndusDesktop }
        default { Stop-IndusStage "unknown stage: $Name" 'other' }
    }
}
