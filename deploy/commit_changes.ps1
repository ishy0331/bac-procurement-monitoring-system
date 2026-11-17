# deploy/commit_changes.ps1 - commit the current version of the system to Git (branch phase3).
#
# Run from the project folder in PowerShell:
#     .\deploy\commit_changes.ps1
#
# What it does:
#   1. Checks that Git is installed and that you are on branch phase3.
#   2. Shows the changed files, then stages everything that .gitignore allows.
#      Never committed: backend\config.py (passwords, secret key), deploy\local_env.ps1, .venv\, uploads\,
#      backup-*\ folders and proctest19.before-*.html backups.
#   3. Stops if backend\config.py or deploy\local_env.ps1 would be committed.
#   4. Commits with a prepared message, then asks before pushing to GitHub.

param([switch]$Yes, [switch]$Push)    # -Yes: no questions; -Push: push after committing
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git is not installed or not on PATH. Install Git for Windows, then run this again." -ForegroundColor Red
    exit 1
}

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "phase3") {
    Write-Host "You are on branch '$branch', not 'phase3'." -ForegroundColor Yellow
    $answer = if ($Yes) { "n" } else { Read-Host "Commit on '$branch' anyway? (y/N)" }
    if ($answer -notmatch '^[Yy]') { Write-Host "Stopped. Nothing was committed."; exit 1 }
}

Write-Host "`nChanged files:" -ForegroundColor Cyan
git status --short
git add -A

$staged = git diff --cached --name-only
if (-not $staged) { Write-Host "`nNothing to commit - everything is already committed." -ForegroundColor Green; exit 0 }

$secret = $staged | Where-Object { $_ -eq "backend/config.py" -or $_ -eq "deploy/local_env.ps1" }
if ($secret) {
    git reset -q
    Write-Host "`nSTOPPED: $($secret -join ', ') would be committed (contains passwords). Nothing was committed." -ForegroundColor Red
    Write-Host "Check that .gitignore lists it, then run 'git rm --cached <file>' if it was committed before."
    exit 1
}

Write-Host "`nFiles to commit ($(@($staged).Count)):" -ForegroundColor Cyan
$staged | ForEach-Object { Write-Host "  $_" }
$answer = if ($Yes) { "y" } else { Read-Host "`nCommit these files? (Y/n)" }
if ($answer -match '^[Nn]') { git reset -q; Write-Host "Stopped. Nothing was committed."; exit 1 }

$msgFile = Join-Path $env:TEMP "bacpms_commit_msg.txt"
@"
Phase 3: workflow, dashboard, admin, documents, exports, deployment

Procurement workflow saved to MariaDB (workflow_api.py): post ITB/RFQ,
record bids, BAC resolution, Notice of Award, contract. One locked
transaction per step, audited, synced to the knowledge graph.

Dashboard computed from the database (dashboard_api.py): KPIs, stages,
timeliness and follow-up, supplier concentration, indicators for review,
completeness and traceability, recent activity.

Super Admin (admin_api.py): user accounts (create with temporary
password, edit, disable, enable) and system settings; real Neo4j
health/verify instead of simulations.

Document management (documents_api.py, migrate_documents.py): upload,
search, download, public release, soft removal; files outside the web
root with SHA-256.

PMR export as Excel (reports_api.py, xlsx_writer.py) and print-ready PDF.

Also included: forgot password (admin-assisted reset), automatic graph
sync after each save (graph_autosync.py), Knowledge Graph Explorer
redesign, Waitress deployment over Tailscale Funnel (serve.py,
DEPLOYMENT.md, deploy/), local vendor assets, encoding form placeholders.

Tests (simulated): t_features.py, t_auth_crud_graph.py,
t_graph_sync.py, t_graph_store.py.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01WQpgTuo9cDj1UikNa3zF4U
"@ | Set-Content -Path $msgFile -Encoding ASCII

git commit -q -F $msgFile
Remove-Item $msgFile -ErrorAction SilentlyContinue
Write-Host "`nCommitted:" -ForegroundColor Green
git log -1 --oneline

$answer = if ($Push) { "y" } elseif ($Yes) { "n" } else { Read-Host "`nPush to GitHub (origin $branch) now? (y/N)" }
if ($answer -match '^[Yy]') {
    git push origin $branch
    if ($LASTEXITCODE -eq 0) { Write-Host "Pushed to GitHub." -ForegroundColor Green }
    else { Write-Host "Push failed. The commit is saved on this laptop; run 'git push origin $branch' later." -ForegroundColor Yellow }
} else {
    Write-Host "Not pushed. Run 'git push origin $branch' when you are ready."
}
