param(
    [ValidateRange(1, 12)][int]$Workers = 3,
    [switch]$NoBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))

function Invoke-Docker([string[]]$Arguments) {
    $previous = $ErrorActionPreference
    try {
        # Windows PowerShell treats native stderr as errors; keep Docker's own exit code.
        $ErrorActionPreference = 'Continue'
        & $script:DockerPath @Arguments
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw "Docker command failed (exit $result): docker $($Arguments -join ' ')" }
}

Push-Location $projectRoot
try {
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $docker) { throw 'Docker Desktop is required. Install it, select Linux containers, start it, and run Start-App.cmd again.' }
    $script:DockerPath = $docker.Source
    Write-Host 'Checking Docker Desktop and Docker Compose ...'
    Invoke-Docker -Arguments @('info', '--format', '{{.ServerVersion}}') | Out-Null
    Invoke-Docker -Arguments @('compose', 'version')
    # Read the resolved mapping so PORT from .env or the shell also changes the browser URL.
    $configurationText = Invoke-Docker -Arguments @('compose', 'config', '--format', 'json')
    $configuration = ($configurationText -join "`n") | ConvertFrom-Json
    $apiPort = [int]$configuration.services.api.ports[0].published
    if ($apiPort -lt 1 -or $apiPort -gt 65535) { throw 'The API published port is missing from the Compose configuration.' }
    Write-Host "Building and starting TicketFlow with $Workers workers ..."
    Invoke-Docker -Arguments @('compose', 'up', '--build', '--scale', "worker=$Workers", '-d', '--wait', '--wait-timeout', '120')
    $url = "http://127.0.0.1:$apiPort"
    $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 10
    Write-Host "TicketFlow is running at $url"
    Write-Host "API documentation: $url/docs"
    Write-Host 'Run Stop-App.cmd to stop it. Bookings remain in the database volume.'
    if (-not $NoBrowser) { Start-Process $url | Out-Null }
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Make sure Docker Desktop is running with Linux containers.'
    Write-Host 'For container details, run: docker compose ps'
    Write-Host 'For recent logs, run: docker compose logs --tail 80'
    exit 1
} finally { Pop-Location }
