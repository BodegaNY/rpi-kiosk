$ErrorActionPreference = "Stop"
$repoPath = Join-Path $PSScriptRoot "classifier-server"
$python = "python"

$conn = Get-NetTCPConnection -LocalPort 8089 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Write-Host "Stopping PID $($conn.OwningProcess) on port 8089..."
    Stop-Process -Id $conn.OwningProcess -Force
    Start-Sleep -Seconds 2
}

Write-Host "Starting classifier in a new window..."
Start-Process -FilePath $python -ArgumentList "server.py" -WorkingDirectory $repoPath
Write-Host "Done. Gallery: http://127.0.0.1:8089/  (Tailscale: http://100.123.231.73:8089/)"
Write-Host "First start may take ~15s while the model loads."
