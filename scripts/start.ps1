param(
    [ValidateRange(1, 12)][int]$Workers = 3,
    [switch]$NoBrowser
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$composeFile = Join-Path $projectRoot 'compose.yaml'
. (Join-Path $PSScriptRoot 'control-common.ps1')
$controlRoot = Join-Path $projectRoot '.runtime\control'
$controllerPath = Join-Path $controlRoot 'controller.json'
$controllerMutex = [Threading.Mutex]::new($false, (Get-TicketFlowControlMutexName $projectRoot))
$operationMutex = [Threading.Mutex]::new($false, ((Get-TicketFlowControlMutexName $projectRoot) + '-launcher'))
$ownsOperation = $false
$sessionId = $null
$controllerStarted = $false
$started = $false

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
    try { $ownsOperation = $operationMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsOperation = $true }
    if (-not $ownsOperation) { throw 'TicketFlow is already starting or stopping. Wait for that operation to finish.' }
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $docker) { throw 'Docker Desktop is required. Install it, select Linux containers, start it, and run Start-App.cmd again.' }
    $script:DockerPath = $docker.Source
    $composeArguments = @('compose', '-p', 'ticketflow', '-f', $composeFile)
    Write-Host 'Checking Docker Desktop and Docker Compose ...'
    Invoke-Docker -Arguments @('info', '--format', '{{.ServerVersion}}') | Out-Null
    Invoke-Docker -Arguments @('compose', 'version')
    # Read the resolved mapping so PORT from .env or the shell also changes the browser URL.
    $configurationText = Invoke-Docker -Arguments ($composeArguments + @('config', '--format', 'json'))
    $configuration = ($configurationText -join "`n") | ConvertFrom-Json
    $apiPort = [int]$configuration.services.api.ports[0].published
    if ($apiPort -lt 1 -or $apiPort -gt 65535) { throw 'The API published port is missing from the Compose configuration.' }
    [void][IO.Directory]::CreateDirectory($controlRoot)
    $oldController = Read-TicketFlowControlFile $controllerPath
    if ($null -ne $oldController -and $null -ne $oldController.PSObject.Properties['session_id']) {
        Write-TicketFlowControlFile (Join-Path $controlRoot 'watcher-stop.json') @{session_id = [string]$oldController.session_id}
    }
    Wait-TicketFlowControlExit $controllerMutex
    $sessionId = [guid]::NewGuid().ToString()
    Write-TicketFlowControlFile $controllerPath @{session_id = $sessionId; port = $apiPort; state = 'starting'; updated_at = 0}
    $watcher = Join-Path $PSScriptRoot 'watch-shutdown.ps1'
    $watcherArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $watcher), '-SessionId', $sessionId, '-Port', [string]$apiPort)
    $windowsPowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
    $controllerProcess = Start-Process -FilePath $windowsPowerShell -ArgumentList $watcherArguments -WindowStyle Hidden -PassThru
    $controllerStarted = $true
    $controllerDeadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        $controllerProcess.Refresh()
        if ($controllerProcess.HasExited) { throw 'The local shutdown controller could not start.' }
        $controller = Read-TicketFlowControlFile $controllerPath
        if ((Test-TicketFlowControlSession $controller $sessionId) -and
            $null -ne $controller.PSObject.Properties['updated_at'] -and [double]$controller.updated_at -gt 0) { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $controllerDeadline)
    if ($null -eq $controller -or [double]$controller.updated_at -le 0) { throw 'The local shutdown controller did not become ready.' }
    Write-Host "Building and starting TicketFlow with $Workers workers ..."
    Invoke-Docker -Arguments ($composeArguments + @('up', '--build', '--scale', "worker=$Workers", '-d', '--wait', '--wait-timeout', '120'))
    $url = "http://127.0.0.1:$apiPort"
    $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 10
    Write-TicketFlowControlFile (Join-Path $controlRoot 'launcher-ready.json') @{session_id = $sessionId}
    $controllerDeadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $controller = Read-TicketFlowControlFile $controllerPath
        if ((Test-TicketFlowControlSession $controller $sessionId) -and [string]$controller.state -eq 'ready') { break }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $controllerDeadline)
    if ($null -eq $controller -or [string]$controller.state -ne 'ready') { throw 'The local shutdown controller did not finish initialization.' }
    $started = $true
    Write-Host "TicketFlow is running at $url"
    Write-Host "API documentation: $url/docs"
    Write-Host 'Use Stop TicketFlow in the dashboard or run Stop-App.cmd. Bookings remain in the database volume.'
    if (-not $NoBrowser) { Start-Process $url | Out-Null }
} catch {
    Write-Host "Startup failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Make sure Docker Desktop is running with Linux containers.'
    Write-Host 'For container details, run: docker compose ps'
    Write-Host 'For recent logs, run: docker compose logs --tail 80'
    exit 1
} finally {
    if ($controllerStarted -and -not $started) {
        try { Write-TicketFlowControlFile (Join-Path $controlRoot 'watcher-stop.json') @{session_id = $sessionId} }
        catch { Write-Warning 'The controller will become unavailable when its heartbeat expires.' }
    }
    if ($ownsOperation) { $operationMutex.ReleaseMutex() }
    $operationMutex.Dispose()
    $controllerMutex.Dispose()
    Pop-Location
}
