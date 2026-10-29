param(
    [Parameter(Mandatory = $true)][guid]$SessionId,
    [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'control-common.ps1')
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$controlRoot = Join-Path $projectRoot '.runtime\control'
$controllerPath = Join-Path $controlRoot 'controller.json'
$session = $SessionId.ToString()
$mutex = [Threading.Mutex]::new($false, (Get-TicketFlowControlMutexName $projectRoot))
$ownsMutex = $false
$state = 'starting'
$errorMessage = $null

function Write-ControllerState {
    $current = Read-TicketFlowControlFile $controllerPath
    if (-not (Test-TicketFlowControlSession $current $session)) { return $false }
    $status = @{session_id = $session; port = $Port; state = $script:state; updated_at = (Get-TicketFlowUnixTime)}
    if ($script:errorMessage) { $status.error = $script:errorMessage }
    Write-TicketFlowControlFile $controllerPath $status
    return $true
}

try {
    try { $ownsMutex = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { exit 0 }
    $docker = Get-Command docker.exe -ErrorAction Stop
    $lastHeartbeat = 0.0
    while ($true) {
        $current = Read-TicketFlowControlFile $controllerPath
        if (-not (Test-TicketFlowControlSession $current $session)) { break }
        $stop = Read-TicketFlowControlFile (Join-Path $controlRoot 'watcher-stop.json')
        if (Test-TicketFlowControlSession $stop $session) { break }
        if ($state -eq 'starting') {
            $ready = Read-TicketFlowControlFile (Join-Path $controlRoot 'launcher-ready.json')
            if (Test-TicketFlowControlSession $ready $session) { $state = 'ready' }
        }
        $now = Get-TicketFlowUnixTime
        if ($now - $lastHeartbeat -ge 1) {
            if (-not (Write-ControllerState)) { break }
            $lastHeartbeat = $now
        }
        if ($state -eq 'ready') {
            $request = Read-TicketFlowControlFile (Join-Path $controlRoot 'shutdown-request.json')
            if ((Test-TicketFlowControlSession $request $session) -and
                $null -ne $request.PSObject.Properties['requested_at']) {
                $requestTime = 0.0
                $validTime = [double]::TryParse([string]$request.requested_at,
                    [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$requestTime)
                if ($validTime -and $requestTime -le $now + 2 -and $now - $requestTime -le 30) {
                    $state = 'stopping'
                    if (-not (Write-ControllerState)) { break }
                    # Only this fixed Compose project can be stopped. No volume
                    # deletion, arbitrary shell commands or PC shutdown is exposed.
                    $previous = $ErrorActionPreference
                    try {
                        $ErrorActionPreference = 'Continue'
                        $output = & $docker.Source compose -p ticketflow -f (Join-Path $projectRoot 'compose.yaml') down 2>&1
                        $result = $LASTEXITCODE
                    } finally { $ErrorActionPreference = $previous }
                    [IO.File]::AppendAllText((Join-Path $controlRoot 'shutdown.log'),
                        "[$([DateTimeOffset]::UtcNow.ToString('o'))] $($output -join [Environment]::NewLine)$([Environment]::NewLine)")
                    if ($result -ne 0) { throw "TicketFlow compose down failed (exit $result). Run Stop-App.cmd to retry." }
                    $state = 'stopped'
                    [void](Write-ControllerState)
                    break
                }
            }
        }
        Start-Sleep -Milliseconds 250
    }
} catch {
    $state = 'error'
    $errorMessage = $_.Exception.Message
    try { [void](Write-ControllerState) } catch { }
    exit 1
} finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
