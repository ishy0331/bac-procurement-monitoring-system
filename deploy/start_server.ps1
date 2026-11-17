# deploy/start_server.ps1 - Start the BAC Procurement Monitoring System (Waitress).
#
# Run from anywhere in PowerShell:
#     .\deploy\start_server.ps1
# Stop with Ctrl+C.
#
# Before the first run: see DEPLOYMENT.md (config.py, local_env.ps1, vendor files).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$activate = Join-Path $root ".venv\Scripts\Activate.ps1"
if (-not (Test-Path $activate)) {
    Write-Error "Virtual environment not found (.venv). See README.md, step 2."
}
. $activate

$envFile = Join-Path $PSScriptRoot "local_env.ps1"
if (Test-Path $envFile) {
    . $envFile
} else {
    Write-Warning "deploy\local_env.ps1 not found: the Neo4j graph sync will report 'not configured'."
}

foreach ($f in @("vendor\tailwind\tailwind-play.js", "vendor\fontawesome\css\all.min.css")) {
    if (-not (Test-Path (Join-Path $root $f))) {
        Write-Warning "$f is missing: pages will look broken. Run .\deploy\get_vendor_assets.ps1 once."
    }
}

python backend\serve.py
