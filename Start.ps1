param([int]$Port = 8776, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$deskRoot = $PSScriptRoot
$deskUrl = "http://127.0.0.1:$Port"
try {
    $deskHealth = Invoke-RestMethod -Uri "$deskUrl/health" -TimeoutSec 3
    if ($deskHealth.service -ne 'AIVideoTranscriber') { throw "Another service is using port $Port." }
    if (-not $NoBrowser) { Start-Process $deskUrl }
    exit 0
} catch {
    $deskListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($deskListener) { throw "Port $Port is occupied. The existing service was not changed." }
}
$deskPython = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $deskPython) { $deskPython = (Get-Command python.exe -ErrorAction Stop).Source }
$deskData = Join-Path $deskRoot 'data'
New-Item -ItemType Directory -Path $deskData -Force | Out-Null
$deskScript = Join-Path $deskRoot 'server.py'
$deskArguments = @(('"' + $deskScript + '"'), '--port', [string]$Port)
Start-Process -FilePath $deskPython -ArgumentList $deskArguments -WorkingDirectory $deskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $deskData 'server.stdout.log') -RedirectStandardError (Join-Path $deskData 'server.stderr.log') | Out-Null
for ($deskAttempt = 0; $deskAttempt -lt 30; $deskAttempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $deskHealth = Invoke-RestMethod -Uri "$deskUrl/health" -TimeoutSec 2
        if ($deskHealth.service -eq 'AIVideoTranscriber') { if (-not $NoBrowser) { Start-Process $deskUrl }; exit 0 }
    } catch {}
}
throw "AI Video Transcriber did not become ready. See data/server.stderr.log."
