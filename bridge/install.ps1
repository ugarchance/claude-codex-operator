# Install agent-bridge for the current user on Windows.
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Machine NAME] [-Language en|tr] [-Python C:\Path\python.exe]
# Copies the scripts to %LOCALAPPDATA%\agent-bridge\bin, writes a starter config (UTF-8 without BOM) if there is
# none, and prints `status`.
param(
    [string]$Machine = $env:COMPUTERNAME.ToLower(),
    [string]$Language = 'en',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    # the py launcher knows the real CPython installs; a Git Bash or MSYS python on PATH would not do
    $Python = (& py -3 -c 'import sys; print(sys.executable)' 2>$null)
    if (-not $Python) { throw 'Python 3.9+ not found: pass -Python C:\Path\python.exe' }
}
$dest = Join-Path $env:LOCALAPPDATA 'agent-bridge\bin'
$conf = Join-Path $env:APPDATA 'agent-bridge'
New-Item -ItemType Directory -Force -Path $dest, $conf | Out-Null
Copy-Item -Force (Join-Path $here 'agent_bridge.py'), (Join-Path $here 'dispatch.py') $dest
$config = Join-Path $conf 'config.json'
if (-not (Test-Path $config)) {
    $json = @{ machine = $Machine; language = $Language } | ConvertTo-Json
    [IO.File]::WriteAllText($config, $json, (New-Object Text.UTF8Encoding $false))
    "wrote $config (add notify_command, codex_model, ... as needed)"
}
"installed: $Python $(Join-Path $dest 'agent_bridge.py')"
& $Python (Join-Path $dest 'agent_bridge.py') status
