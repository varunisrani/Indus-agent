
# ---------------------------------------------------------------------------------------------
# Entry: one stage, the manifest, the resolved paths, or the whole ladder.
# ---------------------------------------------------------------------------------------------

function Invoke-IndusSingleStage([string]$Name) {
    $def = Get-IndusStageDef $Name
    $known = [bool]$def -or (@($script:IndusStages.dispatch_only) -ccontains $Name)
    $asJson = [bool]$script:IndusOpts.Json
    if (-not $known) {
        if ($asJson) { Write-IndusFrame $false $Name $false "unknown stage: $Name" } else { Write-IndusError "unknown stage: $Name" }
        return 2
    }
    if ($def -and $def.needs_user_input -and (Test-IndusNonInteractive)) {
        if ($asJson) { Write-IndusFrame $true $Name $true 'needs user input' } else { Write-IndusInfo "Skipping ${Name}: needs user input" }
        return 0
    }
    try {
        Assert-IndusLayout
        Invoke-IndusStage $Name | Out-Null
    } catch {
        Write-IndusError "$_"
        if ($asJson) { Write-IndusFrame $false $Name $false "$_" }
        return 1
    }
    if ($asJson) { Write-IndusFrame $true $Name $false }
    return 0
}

function Write-IndusClosingNotes {
    if ($script:IndusRunAsFile -and $script:IndusBinWasOnPath -eq $false) {
        Write-IndusInfo 'Open a new terminal to use indus (this window keeps the PATH it started with).'
    }
}

# The full ladder, with exactly one receipt: success, failed (with the stage and its class), or
# interrupted from the finally block when Ctrl-C stopped the run (a no-op after an outcome).
function Invoke-IndusLadder {
    $script:IndusRunId = [guid]::NewGuid().ToString('N')
    $script:IndusStartedAt = Get-IndusUtcStamp
    $script:IndusReceiptWritten = $false
    $script:IndusCurrentStage = 'prerequisites'
    $outcome = 1
    try {
        Assert-IndusLayout
        Write-Host ''
        Write-Host 'Indus Agent installer'
        Write-Host ''
        foreach ($def in @($script:IndusStages.stages)) {
            $script:IndusCurrentStage = [string]$def.name
            $script:IndusFailureClass = ''
            if ($def.needs_user_input -and (Test-IndusNonInteractive)) {
                Write-IndusInfo "Skipping $($def.name): needs user input"
                continue
            }
            Invoke-IndusStage ([string]$def.name) | Out-Null
        }
        Write-IndusReceipt 'success' | Out-Null
        Write-IndusClosingNotes
        $outcome = 0
    } catch {
        Write-IndusReceipt 'failed' $script:IndusCurrentStage (Get-IndusFailureClass) | Out-Null
        Write-IndusError "$_"
        $outcome = 1
    } finally {
        Write-IndusReceipt 'failed' $script:IndusCurrentStage 'interrupted' | Out-Null
    }
    return $outcome
}

function Invoke-IndusEntry {
    $ErrorActionPreference = 'Stop'
    $problem = ConvertFrom-IndusArgs $script:IndusRest
    if ($problem) {
        Write-IndusError $problem.Message
        Write-IndusError 'run with -h for the options'
        $script:IndusExitCode = [int]$problem.Code
        return
    }
    if ($script:IndusOpts.Help) {
        Write-IndusUsage
        $script:IndusExitCode = 0
        return
    }
    $channelProblem = Resolve-IndusChannel
    if ($channelProblem) {
        Write-IndusError $channelProblem
        $script:IndusExitCode = 2
        return
    }
    if ($script:IndusOpts.Manifest) {
        Write-IndusJsonLine (Get-IndusManifestJson)
        $script:IndusExitCode = 0
        return
    }
    Initialize-IndusPaths
    if ($script:IndusOpts.ShowResolvedPaths) {
        Write-IndusJsonLine ($script:IndusPaths.Report | ConvertTo-Json -Depth 5 -Compress)
        $script:IndusExitCode = 0
        return
    }
    $env:UV_NO_CONFIG = '1'
    if ($script:IndusOpts.Verbose) { $env:INDUS_INSTALL_VERBOSE = '1' }
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch {
        Write-Verbose 'TLS 1.2 is already the default here'
    }
    if ($script:IndusOpts.Stage) {
        $script:IndusExitCode = [int](@(Invoke-IndusSingleStage ([string]$script:IndusOpts.Stage)) | Select-Object -Last 1)
        return
    }
    $script:IndusExitCode = [int](@(Invoke-IndusLadder) | Select-Object -Last 1)
}

# Dot-sourced: definitions only. A script file exits with the code; text run through iex or a
# scriptblock leaves it in $LASTEXITCODE so the person's window stays open.
if (-not $script:IndusDotSourced) {
    Invoke-IndusEntry | Out-Null
    if ($script:IndusRunAsFile) { exit $script:IndusExitCode }
    $global:LASTEXITCODE = $script:IndusExitCode
}
