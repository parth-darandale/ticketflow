Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $projectRoot
try {
    $docker = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($null -eq $docker) { throw 'Docker Desktop is required to stop the Compose services.' }
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $docker.Source compose down
        $result = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($result -ne 0) { throw "docker compose down failed (exit $result). Start Docker Desktop and try again." }
    Write-Host 'TicketFlow is stopped. The database volume and its bookings are preserved.'
} catch {
    Write-Host "Stop failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally { Pop-Location }
