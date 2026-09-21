[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("up", "down", "update", "smoke", "logs", "ps", "help", "check-read")]
    [string]$Command = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot "deploy/compose.yaml"
$ComposeOverride = if ($env:BB_COMPOSE_OVERRIDE) {
    if ([System.IO.Path]::IsPathRooted($env:BB_COMPOSE_OVERRIDE)) { $env:BB_COMPOSE_OVERRIDE } else { Join-Path $ProjectRoot $env:BB_COMPOSE_OVERRIDE }
} else { $null }
$Profile = if ($env:BB_PROFILE) { $env:BB_PROFILE } else { "dev" }

function Invoke-Compose {
    $Files = @("--file", $ComposeFile)
    if ($ComposeOverride) { $Files += @("--file", $ComposeOverride) }
    & docker compose --project-directory $ProjectRoot @Files --profile $Profile @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Invoke-ComposeProfiles {
    $Files = @("--file", $ComposeFile)
    if ($ComposeOverride) { $Files += @("--file", $ComposeOverride) }
    & docker compose --project-directory $ProjectRoot @Files --profile dev --profile build @args
    if ($LASTEXITCODE -ne 0) { throw "docker compose failed with exit code $LASTEXITCODE" }
}

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker is required"
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) { throw "docker compose is unavailable" }
}

function Remove-DynamicWorkers {
    $WorkerIds = @(& docker ps -aq --filter "label=bb.vm_id")
    if ($LASTEXITCODE -ne 0) { throw "docker ps failed" }
    if ($WorkerIds.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace(($WorkerIds -join ""))) {
        Write-Host "Removing dynamic BlackBox workers so they are recreated from the current images..."
        & docker rm -f $WorkerIds *> $null
        if ($LASTEXITCODE -ne 0) { throw "docker rm failed" }
    }
}

function Invoke-GitFastForward {
    & git -C $ProjectRoot diff --quiet
    $WorktreeDirty = $LASTEXITCODE -eq 1
    if ($LASTEXITCODE -gt 1) { throw "git diff failed" }
    & git -C $ProjectRoot diff --cached --quiet
    $IndexDirty = $LASTEXITCODE -eq 1
    if ($LASTEXITCODE -gt 1) { throw "git diff --cached failed" }
    if ($WorktreeDirty -or $IndexDirty) {
        Write-Host "Stashing local tracked changes so git pull can fast-forward..."
        & git -C $ProjectRoot stash push -m "bbctl-update autostash"
        if ($LASTEXITCODE -ne 0) { throw "git stash failed" }
        Write-Host "Local changes kept in git stash. Inspect with: git stash list"
    }
    & git -C $ProjectRoot pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull failed" }
}

function Invoke-Smoke {
    if ($Profile -eq "legacy") {
        $LegacyPort = if ($env:BB_HTTP_PORT) { $env:BB_HTTP_PORT } else { "5000" }
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$LegacyPort/" | Out-Null
        Write-Host "Legacy smoke check passed: http://127.0.0.1:$LegacyPort/"
        return
    }
    foreach ($Attempt in 1..30) {
        try {
            $HubPort = if ($env:BB_HUB_PORT) { $env:BB_HUB_PORT } else { "8080" }
            Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$HubPort/healthz" | Out-Null
            Invoke-Compose exec -T hub /app/.venv/bin/python -m services.hub.smoke --url http://127.0.0.1:8080 --data-root /data
            return
        } catch {
            if ($Attempt -eq 30) { throw "Web endpoint did not become ready on port $HubPort" }
            Start-Sleep -Seconds 1
        }
    }
}

if ($Command -eq "help") {
    Write-Host "Usage: ./bbctl.ps1 <up|down|update|smoke|check-read|logs|ps|help>"
    exit 0
}

Assert-Docker
switch ($Command) {
    "up" { Invoke-ComposeProfiles build; Invoke-Compose up --detach --build }
    "down" { Invoke-Compose stop; Remove-DynamicWorkers; Invoke-Compose down }
    "update" {
        if ($env:BBCTL_UPDATE_APPLY -ne "1") {
            Invoke-GitFastForward
            $env:BBCTL_UPDATE_APPLY = "1"
            & $PSCommandPath update
            exit $LASTEXITCODE
        }
        Invoke-ComposeProfiles build --pull
        Invoke-Compose stop
        Remove-DynamicWorkers
        Invoke-Compose down
        Invoke-Compose up --detach --remove-orphans
        Invoke-Smoke
    }
    "smoke" { Invoke-Smoke }
    "logs" { Invoke-Compose logs --follow }
    "ps" { Invoke-Compose ps }
    "check-read" {
        $ProbeArgs = if ($Rest -and $Rest.Count -gt 0) { $Rest } else { @("--list") }
        Invoke-Compose exec -T hub /app/.venv/bin/python -m services.hub.check_read @ProbeArgs
    }
}
