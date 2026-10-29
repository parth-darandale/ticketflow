# Shared by the Windows launchers and the local shutdown watcher.
function Get-TicketFlowControlMutexName([string]$ProjectRoot) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($ProjectRoot).ToLowerInvariant())
        $hash = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')
    } finally { $sha.Dispose() }
    return "Local\TicketFlowControl-$hash"
}

function Read-TicketFlowControlFile([string]$Path) {
    try { return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json }
    catch { return $null }
}

function Test-TicketFlowControlSession($Document, [string]$SessionId) {
    return $null -ne $Document -and $null -ne $Document.PSObject.Properties['session_id'] -and
        [string]$Document.session_id -ceq $SessionId
}

function Get-TicketFlowUnixTime {
    return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
}

function Write-TicketFlowControlFile([string]$Path, $Value) {
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Compress -Depth 8), [Text.UTF8Encoding]::new($false))
    try {
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            try {
                if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
                else { [IO.File]::Move($temporary, $Path) }
                return
            } catch [IO.IOException] {
                if ($attempt -eq 4) { throw }
                Start-Sleep -Milliseconds 40
            }
        }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

function Wait-TicketFlowControlExit([Threading.Mutex]$Mutex, [int]$Seconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        $held = $false
        try {
            try { $held = $Mutex.WaitOne(0) }
            catch [Threading.AbandonedMutexException] { $held = $true }
            if ($held) { return }
        } finally { if ($held) { $Mutex.ReleaseMutex() } }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The previous TicketFlow shutdown controller is still busy. Wait for shutdown to finish and try again.'
}
