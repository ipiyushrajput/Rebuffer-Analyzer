<#
.SYNOPSIS
    One-time setup for RBA on Windows.

.DESCRIPTION
    Creates the backend virtualenv, installs the backend and frontend dependencies,
    installs the Playwright browser used for PDF export, and writes backend\.env from
    the example if it is not there yet.

    Nothing here binds a port and nothing is written outside the repository, so the
    script is safe to re-run: it brings an existing checkout up to date.

.PARAMETER Dev
    Also install the backend development extras (pytest, ruff, mypy). Use this when you
    intend to run `run.ps1 test` or `run.ps1 lint`.

.PARAMETER SkipBrowser
    Skip the Playwright Chromium download (~150 MB). PDF export is then unavailable and
    HTML export still works; `run.ps1 setup` can install it later.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -Dev
#>
[CmdletBinding()]
param(
    [switch]$Dev,
    [switch]$SkipBrowser
)

$ErrorActionPreference = 'Stop'

. "$PSScriptRoot\_common.ps1"

Write-Step 'Checking prerequisites'

$python = Get-PythonLauncher
Write-Detail "Python  : $(& $python.File @($python.Args + '--version') 2>&1)"

Assert-Command -Name 'node' -Hint 'Install Node.js 20 LTS or newer from https://nodejs.org.'
Write-Detail "Node    : $(node --version)"
Write-Detail "npm     : $(npm --version)"

if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Write-Detail "ffmpeg  : $((ffmpeg -version | Select-Object -First 1))"
} else {
    Write-Note 'ffmpeg is absent. The decode-error and quality detectors do not run without it; every other check does. Install it with: winget install Gyan.FFmpeg'
}

# --- backend ----------------------------------------------------------------

$venv = Join-Path $RepoRoot 'backend\.venv'
if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
    Write-Step 'Creating the backend virtualenv'
    & $python.File @($python.Args + @('-m', 'venv', $venv))
    Assert-LastExit 'Creating the virtualenv failed.'
} else {
    Write-Step 'The backend virtualenv is already present'
}

$venvPython = Join-Path $venv 'Scripts\python.exe'

Write-Step 'Installing the backend'
& $venvPython -m pip install --upgrade pip --quiet
Assert-LastExit 'Upgrading pip failed.'

$target = if ($Dev) { 'backend[dev]' } else { 'backend' }
Push-Location $RepoRoot
try {
    & $venvPython -m pip install -e $target
    Assert-LastExit "Installing $target failed."
} finally {
    Pop-Location
}

if (-not $SkipBrowser) {
    Write-Step 'Installing the Playwright browser for PDF export'
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Note 'The Playwright browser did not install. PDF export is unavailable until it does; HTML export works regardless.'
    }
}

# --- environment file -------------------------------------------------------

$envFile = Join-Path $RepoRoot 'backend\.env'
if (-not (Test-Path $envFile)) {
    Write-Step 'Writing backend\.env'
    Copy-Item (Join-Path $RepoRoot 'backend\.env.example') $envFile
    Write-Note "backend\.env was created from the example. Fill in DB_* before starting, or set DB_ENGINE=sqlite to run against a local file. It is git-ignored and must stay that way."
} else {
    Write-Step 'backend\.env is already present'
}

# --- frontend ---------------------------------------------------------------

Write-Step 'Installing the frontend'
Push-Location (Join-Path $RepoRoot 'frontend')
try {
    if (Test-Path 'package-lock.json') { npm ci } else { npm install }
    Assert-LastExit 'Installing the frontend dependencies failed.'
} finally {
    Pop-Location
}

Write-Host ''
Write-Step 'Setup complete'
Write-Host '  Start both services : ' -NoNewline; Write-Host '.\deploy\windows\run.ps1 dev' -ForegroundColor Cyan
Write-Host '  Backend only        : ' -NoNewline; Write-Host '.\deploy\windows\run.ps1 backend' -ForegroundColor Cyan
Write-Host '  Analyse one channel : ' -NoNewline; Write-Host '.\deploy\windows\run.ps1 analyse <url>' -ForegroundColor Cyan
Write-Host ''
