# deploy/get_vendor_assets.ps1 - Download Tailwind and Font Awesome into vendor/
#
# Run ONCE while connected to the internet:
#     .\deploy\get_vendor_assets.ps1
# Afterwards the pages work without internet. Commit the vendor\ folder so a
# fresh clone also works offline.
#
#   Tailwind:      the same Play CDN script the page used before (v3), saved locally,
#                  so the page looks exactly as it did. (Tailwind, MIT licence.)
#   Font Awesome:  Free 6.4.0, the version the page used before. (Icons CC BY 4.0,
#                  fonts SIL OFL 1.1, code MIT.)

$ErrorActionPreference = "Stop"
$root   = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"

# --- Tailwind (Play CDN script) ---
$twDir = Join-Path $vendor "tailwind"
New-Item -ItemType Directory -Force -Path $twDir | Out-Null
Write-Host "Downloading Tailwind..."
Invoke-WebRequest -Uri "https://cdn.tailwindcss.com" -OutFile (Join-Path $twDir "tailwind-play.js") -UseBasicParsing

# --- Font Awesome Free 6.4.0 ---
$faVersion = "6.4.0"
$faDir = Join-Path $vendor "fontawesome"
$zip   = Join-Path $env:TEMP "fontawesome-free-$faVersion-web.zip"
$tmp   = Join-Path $env:TEMP "fontawesome-free-$faVersion-web"
Write-Host "Downloading Font Awesome $faVersion..."
Invoke-WebRequest -Uri "https://use.fontawesome.com/releases/v$faVersion/fontawesome-free-$faVersion-web.zip" -OutFile $zip -UseBasicParsing
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
Expand-Archive -Path $zip -DestinationPath $env:TEMP -Force

if (Test-Path $faDir) { Remove-Item -Recurse -Force $faDir }
New-Item -ItemType Directory -Force -Path (Join-Path $faDir "css") | Out-Null
Copy-Item (Join-Path $tmp "css\all.min.css") (Join-Path $faDir "css\all.min.css")
Copy-Item (Join-Path $tmp "webfonts") (Join-Path $faDir "webfonts") -Recurse
Copy-Item (Join-Path $tmp "LICENSE.txt") (Join-Path $faDir "LICENSE.txt")
Remove-Item -Recurse -Force $tmp
Remove-Item -Force $zip

Write-Host ""
Write-Host "Done. Files saved in: $vendor"
Get-ChildItem -Recurse $vendor -File | Select-Object @{n="File";e={$_.FullName.Substring($root.Length + 1)}}, Length | Format-Table -AutoSize
