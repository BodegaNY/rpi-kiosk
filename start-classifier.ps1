$ErrorActionPreference = "Stop"

$repoPath = "D:\Projects\scripts\rpi-kiosk\classifier-server"
$python = "python"
$server = "server.py"

Set-Location $repoPath

# Start the classifier server in the current console session.
& $python $server
