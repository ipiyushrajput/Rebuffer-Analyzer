<#
.SYNOPSIS
    The Windows equivalent of the Makefile.

.DESCRIPTION
    One entry point for every task: run the stack, run the tests, build the bundle, or
    analyse a channel headlessly. Ports come from RBA_PORT and RBA_FRONTEND_PORT and are
    checked before anything binds; port 8001 is refused because it belongs to the existing
    Metanalyser backend.

.PARAMETER Command
    dev       Backend and frontend together, both with reload. The default.
    backend   Backend only, on RBA_PORT (8010).
    frontend  Vite dev server only, on 5173, proxying /api and /ws to the backend.
    serve     Production bundle: builds the frontend, then serves it on RBA_FRONTEND_PORT
              (8080) next to the backend. This is the Windows stand-in for nginx.
    build     Build the frontend bundle only.
    test      pytest + vitest.
    lint      ruff + mypy + eslint + tsc.
    rules     Regenerate docs\RULES.md from the rule registry.
    analyse   Analyse one channel headlessly. Pass the URL and any CLI flags after it.
    setup     Re-run the installer (deploy\windows\setup.ps1).
    clean     Remove build and cache artefacts.

.EXAMPLE
    .\deploy\windows\run.ps1 dev

.EXAMPLE
    .\deploy\windows\run.ps1 analyse "https://cdn.example/live/ch1/master.m3u8" --duration 5m --html out.html
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('dev', 'backend', 'frontend', 'serve', 'build', 'test', 'lint', 'rules', 'analyse', 'setup', 'clean', 'help')]
    [string]$Command = 'dev',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = 'Stop'

. "$PSScriptRoot\_common.ps1"

$BackendPort = if ($env:RBA_PORT) { [int]$env:RBA_PORT } else { 8010 }
$FrontendPort = if ($env:RBA_FRONTEND_PORT) { [int]$env:RBA_FRONTEND_PORT } else { 8080 }

function Start-Child {
    param(
        [string]$File,
        [string[]]$Arguments,
        [string]$WorkingDirectory
    )
    return Start-Process -FilePath $File -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru
}

# npm and uvicorn both spawn children, so the whole tree goes rather than the parent alone.
# Stop-Process is the fallback: it takes the parent only, which still beats orphaning it.
function Stop-Tree {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process -or $Process.HasExited) { return }
    if (Get-Command taskkill.exe -ErrorAction SilentlyContinue) {
        & taskkill.exe /PID $Process.Id /T /F *> $null
        if ($LASTEXITCODE -eq 0) { return }
    }
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
}

<# Runs the given processes until one exits or Ctrl+C arrives, then stops all of them. #>
function Wait-Children {
    param([System.Diagnostics.Process[]]$Processes)
    try {
        while ($true) {
            foreach ($process in $Processes) {
                if ($process.HasExited) { return }
            }
            Start-Sleep -Milliseconds 400
        }
    } finally {
        Write-Host ''
        Write-Step 'Stopping'
        foreach ($process in $Processes) { Stop-Tree $process }
    }
}

switch ($Command) {

    'help' {
        # Printed rather than pulled from the comment block: Get-Help does not find help
        # for a script invoked by path, and an empty help screen is worse than none.
        Write-Host ''
        Write-Host '  RBA on Windows' -ForegroundColor White
        Write-Host '  rba.cmd <command> [arguments]' -ForegroundColor DarkGray
        Write-Host ''
        $commands = [ordered]@{
            'dev'      = "backend on $BackendPort and the Vite dev server on 5173, both with reload"
            'backend'  = "backend only, on RBA_PORT ($BackendPort)"
            'frontend' = 'Vite dev server only, on 5173'
            'serve'    = "build the bundle and serve it on RBA_FRONTEND_PORT ($FrontendPort) with the backend"
            'build'    = 'build the frontend bundle only'
            'test'     = 'pytest + vitest'
            'lint'     = 'ruff + mypy + eslint + tsc'
            'rules'    = 'regenerate docs\RULES.md from the rule registry'
            'analyse'  = 'analyse one channel headlessly; pass the URL and any CLI flags'
            'setup'    = 're-run the installer'
            'clean'    = 'remove build and cache artefacts'
        }
        foreach ($name in $commands.Keys) {
            Write-Host ('    {0,-10}' -f $name) -ForegroundColor Cyan -NoNewline
            Write-Host $commands[$name]
        }
        Write-Host ''
        Write-Host '  Examples' -ForegroundColor White
        Write-Host '    rba.cmd dev'
        Write-Host '    rba.cmd analyse "https://cdn.example/live/ch1/master.m3u8" --duration 5m --html out.html'
        Write-Host '    $env:RBA_PORT = 8011; rba.cmd serve'
        Write-Host ''
        Write-Host '  Full setup guide: docs\SETUP.md' -ForegroundColor DarkGray
        Write-Host ''
    }

    'setup' {
        & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup.ps1') @Rest
    }

    'backend' {
        $python = Get-VenvPython
        Assert-PortFree -Port $BackendPort -Role 'backend'
        Write-Step "Backend on http://127.0.0.1:$BackendPort — health at /api/health"
        Push-Location (Join-Path $RepoRoot 'backend')
        try {
            & $python -m uvicorn app.main:app --host 0.0.0.0 --port $BackendPort --reload
        } finally {
            Pop-Location
        }
    }

    'frontend' {
        Assert-PortFree -Port 5173 -Role 'frontend dev server'
        Write-Step 'Frontend on http://127.0.0.1:5173'
        Push-Location (Join-Path $RepoRoot 'frontend')
        try {
            npm run dev
        } finally {
            Pop-Location
        }
    }

    'dev' {
        $python = Get-VenvPython
        Assert-PortFree -Port $BackendPort -Role 'backend'
        Assert-PortFree -Port 5173 -Role 'frontend dev server'

        Write-Step "Backend  : http://127.0.0.1:$BackendPort/api/health"
        Write-Step 'Frontend : http://127.0.0.1:5173'
        Write-Detail 'Ctrl+C stops both.'

        $backend = Start-Child -File $python `
            -Arguments @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$BackendPort", '--reload') `
            -WorkingDirectory (Join-Path $RepoRoot 'backend')

        $frontend = Start-Child -File 'cmd.exe' `
            -Arguments @('/c', 'npm', 'run', 'dev') `
            -WorkingDirectory (Join-Path $RepoRoot 'frontend')

        Wait-Children -Processes @($backend, $frontend)
    }

    'build' {
        Write-Step 'Building the frontend bundle'
        Push-Location (Join-Path $RepoRoot 'frontend')
        try {
            npm run build
            Assert-LastExit 'The frontend build failed.'
        } finally {
            Pop-Location
        }
    }

    'serve' {
        $python = Get-VenvPython
        Assert-PortFree -Port $BackendPort -Role 'backend'
        Assert-PortFree -Port $FrontendPort -Role 'frontend'

        Write-Step 'Building the frontend bundle'
        Push-Location (Join-Path $RepoRoot 'frontend')
        try {
            npm run build
            Assert-LastExit 'The frontend build failed.'
        } finally {
            Pop-Location
        }

        Write-Step "Backend  : http://127.0.0.1:$BackendPort/api/health"
        Write-Step "Frontend : http://127.0.0.1:$FrontendPort"
        Write-Detail 'Ctrl+C stops both.'

        # No --reload here: jobs are held in process, and a reload would restart them.
        $backend = Start-Child -File $python `
            -Arguments @('-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', "$BackendPort") `
            -WorkingDirectory (Join-Path $RepoRoot 'backend')

        $frontend = Start-Child -File 'cmd.exe' `
            -Arguments @('/c', 'npm', 'run', 'preview', '--', '--port', "$FrontendPort", '--host') `
            -WorkingDirectory (Join-Path $RepoRoot 'frontend')

        Wait-Children -Processes @($backend, $frontend)
    }

    'test' {
        $python = Get-VenvPython
        Write-Step 'Backend tests'
        Push-Location (Join-Path $RepoRoot 'backend')
        try {
            & $python -m pytest -q
            Assert-LastExit 'The backend tests failed.'
        } finally {
            Pop-Location
        }

        Write-Step 'Frontend tests'
        Push-Location (Join-Path $RepoRoot 'frontend')
        try {
            npm run test -- --run
            Assert-LastExit 'The frontend tests failed.'
        } finally {
            Pop-Location
        }
    }

    'lint' {
        $python = Get-VenvPython
        Write-Step 'ruff and mypy'
        Push-Location (Join-Path $RepoRoot 'backend')
        try {
            & $python -m ruff check app tests;        Assert-LastExit 'ruff reported findings.'
            & $python -m ruff format --check app tests; Assert-LastExit 'ruff format reported findings.'
            & $python -m mypy;                        Assert-LastExit 'mypy reported findings.'
        } finally {
            Pop-Location
        }

        Write-Step 'eslint and tsc'
        Push-Location (Join-Path $RepoRoot 'frontend')
        try {
            npm run lint;      Assert-LastExit 'eslint reported findings.'
            npx tsc --noEmit;  Assert-LastExit 'tsc reported findings.'
        } finally {
            Pop-Location
        }
    }

    'rules' {
        $python = Get-VenvPython
        Write-Step 'Regenerating docs\RULES.md'
        Push-Location (Join-Path $RepoRoot 'backend')
        try {
            $output = Join-Path $RepoRoot 'docs\RULES.md'
            & $python -m app.cli rules --markdown | Set-Content -Path $output -Encoding utf8
            Assert-LastExit 'Generating the rule catalogue failed.'
        } finally {
            Pop-Location
        }
        Write-Detail 'docs\RULES.md regenerated'
    }

    'analyse' {
        if ($Rest.Count -eq 0) {
            Write-Fail 'Pass the playback URL, for example: .\deploy\windows\run.ps1 analyse "https://cdn.example/live/ch1/master.m3u8" --duration 5m --html out.html'
        }
        $python = Get-VenvPython
        Push-Location (Join-Path $RepoRoot 'backend')
        try {
            & $python -m app.cli analyse @Rest
            # 0 means no stream-side defect and 2 means one was found; both are results,
            # so the exit code is passed through rather than treated as a failure.
            exit $LASTEXITCODE
        } finally {
            Pop-Location
        }
    }

    'clean' {
        Write-Step 'Removing build and cache artefacts'
        $paths = @(
            'backend\.pytest_cache', 'backend\.mypy_cache', 'backend\.ruff_cache', 'frontend\dist'
        )
        foreach ($path in $paths) {
            $full = Join-Path $RepoRoot $path
            if (Test-Path $full) { Remove-Item -Recurse -Force $full }
        }
        Get-ChildItem -Path (Join-Path $RepoRoot 'backend') -Filter '__pycache__' -Recurse -Directory -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force
        Write-Detail 'done'
    }
}
