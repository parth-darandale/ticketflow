Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'control-common.ps1')
$controlRoot = Join-Path $projectRoot '.runtime\control'
$controllerPath = Join-Path $controlRoot 'controller.json'
$controllerMutex = [Threading.Mutex]::new($false, (Get-TicketFlowControlMutexName $projectRoot))
$operationMutex = [Threading.Mutex]::new($false, ((Get-TicketFlowControlMutexName $projectRoot) + '-launcher'))
$ownsOperation = $false
Push-Location $projectRoot
try {
    try { $ownsOperation = $operationMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsOperation = $true }
    if (-not $ownsOperation) { throw 'TicketFlow is already starting or stopping. Wait for that operation to finish.' }
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $docker) { throw 'Docker Desktop is required to stop the Compose services.' }
    $controller = Read-TicketFlowControlFile $controllerPath
    if ($null -ne $controller -and $null -ne $controller.PSObject.Properties['session_id']) {
        Write-TicketFlowControlFile (Join-Path $controlRoot 'watcher-stop.json') @{session_id = [string]$controller.session_id}
    }
    Wait-TicketFlowControlExit $controllerMutex
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $docker.Source compose -p ticketflow -f (Join-Path $projectRoot 'compose.yaml') down
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw "docker compose down failed (exit $result). Start Docker Desktop and try again." }
    if ($null -ne $controller -and $null -ne $controller.PSObject.Properties['session_id']) {
        Write-TicketFlowControlFile $controllerPath @{session_id = [string]$controller.session_id; port = $controller.port;
            state = 'stopped'; updated_at = (Get-TicketFlowUnixTime)}
    }
    Write-Host 'TicketFlow is stopped. The database volume and its bookings are preserved.'
} catch {
    Write-Host "Stop failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($ownsOperation) { $operationMutex.ReleaseMutex() }
    $operationMutex.Dispose()
    $controllerMutex.Dispose()
    Pop-Location
}
