<#
    Shared helpers for the Windows scripts.

    Dot-sourced by setup.ps1 and run.ps1; it defines $RepoRoot and the small set of checks
    both of them need. It runs nothing on its own.
#>

Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$RepoRoot = $script:RepoRoot

# Port 8001 belongs to the existing Metanalyser backend and is never taken by RBA.
$script:ReservedPort = 8001

function Write-Step {
    param([string]$Message)
    Write-Host '==> ' -ForegroundColor Blue -NoNewline
    Write-Host $Message
}

function Write-Detail {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Write-Note {
    param([string]$Message)
    Write-Host 'note: ' -ForegroundColor Yellow -NoNewline
    Write-Host $Message
}

function Write-Fail {
    param([string]$Message)
    Write-Host 'error: ' -ForegroundColor Red -NoNewline
    Write-Host $Message
    exit 1
}

function Assert-LastExit {
    param([string]$Message)
    if ($LASTEXITCODE -ne 0) { Write-Fail $Message }
}

function Assert-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Fail "$Name is not on PATH. $Hint"
    }
}

<#
    Windows ships three ways to start Python - `py -3`, `python`, and a Store stub that only
    opens the Store. This returns the first one that actually reports a version, as a file
    plus its leading arguments, so callers can splat it.
#>
function Get-PythonLauncher {
    $candidates = @(
        @{ File = 'py';     Args = @('-3') },
        @{ File = 'python'; Args = @() },
        @{ File = 'python3'; Args = @() }
    )
    foreach ($candidate in $candidates) {
        $command = Get-Command $candidate.File -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            $version = & $candidate.File @($candidate.Args + '--version') 2>&1
        } catch {
            continue
        }
        # The version string is the test, not $LASTEXITCODE: under Set-StrictMode that
        # automatic variable does not exist until a native command has run in the session.
        if ("$version" -match 'Python 3\.(\d+)') {
            if ([int]$Matches[1] -lt 11) {
                Write-Fail "Python 3.11 or newer is required; $version was found. Install it from https://python.org or with: winget install Python.Python.3.12"
            }
            return $candidate
        }
    }
    Write-Fail 'Python 3.11 or newer is not on PATH. Install it from https://python.org or with: winget install Python.Python.3.12 (tick "Add python.exe to PATH").'
}

function Get-VenvPython {
    $python = Join-Path $RepoRoot 'backend\.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        Write-Fail 'The backend virtualenv is missing. Run: powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1'
    }
    return $python
}

function Test-PortFree {
    param([int]$Port)
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return -not $listener
}

<#
    Refuses the reserved port outright and reports who holds a bound one, so the next free
    port is chosen deliberately rather than bound over.
#>
function Assert-PortFree {
    param([int]$Port, [string]$Role)
    if ($Port -eq $script:ReservedPort) {
        Write-Fail "Port $script:ReservedPort belongs to the Metanalyser backend and is never used by RBA."
    }
    if (Test-PortFree -Port $Port) { return }

    $owner = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $who = 'another process'
    if ($owner) {
        $process = Get-Process -Id $owner.OwningProcess -ErrorAction SilentlyContinue
        if ($process) { $who = "$($process.ProcessName) (PID $($process.Id))" }
    }
    Write-Fail "Port $Port ($Role) is already bound by $who. Take the next free port - set RBA_PORT or RBA_FRONTEND_PORT - and record it in README.md and backend\.env."
}
