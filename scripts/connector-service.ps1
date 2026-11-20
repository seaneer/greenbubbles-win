param(
    [ValidateSet('start', 'stop', 'restart', 'status')]
    [string]$Action = 'status',
    [string]$BindHost = '127.0.0.1',
    [int]$Port = 8787
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$PortablePython = Join-Path $Root 'python\python.exe'
if (Test-Path -LiteralPath $PortablePython) { $Python = $PortablePython }
$PidFile = Join-Path $Root 'state\connector.pid'
$LogDir = Join-Path $Root 'state\logs'
$Launcher = Join-Path $Root 'greenbubbles.py'

function Test-ConnectorProcess {
    param([int]$ProcessId)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    $cmdline = [string]$process.CommandLine
    return ($cmdline -match 'greenbubbles\.py') -and ($cmdline -match '\bserve\b')
}

function Get-ConnectorPid {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if (-not $raw) { return $null }
    try { $candidate = [int]$raw } catch { return $null }
    if (Test-ConnectorProcess $candidate) { return $candidate }
    # PID was reused by an unrelated process; drop the stale file instead of
    # reporting or stopping that process.
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Test-Health {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return [bool]$response.ok
    } catch {
        return $false
    }
}

function Show-Status {
    $processId = Get-ConnectorPid
    if (-not $processId) {
        [Console]::WriteLine("stopped (no live PID file)")
        return $false
    }
    $healthy = Test-Health
    $portState = if ($healthy) { 'accepting' } else { 'not accepting' }
    [Console]::WriteLine(("running pid={0} port={1} health={2} ({3})" -f $processId, $Port, $healthy, $portState))
    return $true
}

function Stop-Connector {
    $processId = Get-ConnectorPid
    if (-not $processId) {
        Write-Output "not running"
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Process -Id $processId -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Write-Output "stopped pid=$processId"
}

function Start-Connector {
    if (-not (Test-Path -LiteralPath $Python)) {
        throw ".venv not found at $Python; create it and install requirements first."
    }

    $existing = Get-ConnectorPid
    if ($existing) {
        if (Test-Health) {
            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId=$existing" -ErrorAction SilentlyContinue).CommandLine
            if ($cmdline -and ($cmdline -notmatch [regex]::Escape("--host $BindHost"))) {
                Write-Output "already running pid=$existing with a different bind; stop it first"
                Write-Output "  $cmdline"
                return
            }
            Write-Output "already running pid=$existing"
            return
        }
        Write-Output "pid=$existing is alive but not healthy; restarting"
        Stop-Connector
    }

    $occupied = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($occupied) {
        throw "port $Port is already in use by pid=$($occupied[0].OwningProcess)"
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdout = Join-Path $LogDir "connector-$stamp.log"
    $stderr = Join-Path $LogDir "connector-$stamp.err.log"

    $arguments = @($Launcher, 'serve', '--host', $BindHost, '--port', "$Port")
    $proc = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $Root `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

    Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding utf8

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-Health) {
            Write-Output ("started pid={0} bind={1}:{2} log={3}" -f $proc.Id, $BindHost, $Port, $stdout)
            return
        }
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }

    Write-Output "start failed; last stderr lines:"
    if (Test-Path -LiteralPath $stderr) { Get-Content -LiteralPath $stderr -Tail 20 }
    Stop-Connector
    exit 1
}

switch ($Action) {
    'start' { Start-Connector }
    'stop' { Stop-Connector }
    'restart' { Stop-Connector; Start-Sleep -Seconds 1; Start-Connector }
    'status' { if (-not (Show-Status)) { exit 1 } }
}
