<#
.SYNOPSIS
    TigerLiteCode - smart setup script for Windows (PowerShell).

.DESCRIPTION
    Verifies the toolchain, installs the Python engine, and builds the
    terminal UI. Re-runnable: skips dependency install when present unless
    you pass -Force.

.PARAMETER Force
    Reinstall UI dependencies even if node_modules already exists.

.EXAMPLE
    ./build.ps1
.EXAMPLE
    ./build.ps1 -Force
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = "Stop"

function Info($m) { Write-Host "==> $m" -ForegroundColor DarkYellow }
function Ok($m)   { Write-Host "  ok $m" -ForegroundColor Green }
function Die($m)  { Write-Host "error $m" -ForegroundColor Red; exit 1 }

# Move to the script's directory (repo root)
Set-Location -Path $PSScriptRoot

Write-Host "`nTigerLiteCode setup`n" -ForegroundColor DarkYellow

# --- detect Python 3.11+ ----------------------------------------------------
$python = $null
foreach ($cand in @("python", "python3", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        $argList = if ($cand -eq "py") { @("-3", "-c") } else { @("-c") }
        try {
            & $cand @argList "import sys; sys.exit(0 if sys.version_info[:2] >= (3,11) else 1)"
            if ($LASTEXITCODE -eq 0) { $python = $cand; break }
        } catch { }
    }
}
if (-not $python) {
    Die "Python 3.11+ is required but was not found. Install it from https://www.python.org/downloads/"
}
$pyArgs = if ($python -eq "py") { @("-3") } else { @() }
Ok ("Python: " + (& $python @pyArgs --version 2>&1))

# --- detect Node 18+ --------------------------------------------------------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Die "Node.js 18+ is required but was not found. Install it from https://nodejs.org/"
}
$nodeMajor = [int](& node -p "process.versions.node.split('.')[0]")
if ($nodeMajor -lt 18) { Die "Node.js 18+ is required (found $(node --version))." }
Ok ("Node.js: " + (node --version))

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Die "npm is required but was not found (it ships with Node.js)."
}
Ok ("npm: " + (npm --version))

Write-Host ""

# --- install the Python engine ----------------------------------------------
Info "Installing the Python engine (pip install -e .)"
& $python @pyArgs -m pip install -e .
if ($LASTEXITCODE -ne 0) { Die "pip install failed. See the output above." }
Ok "Engine installed."

Write-Host ""

# --- build the terminal UI --------------------------------------------------
if (-not (Test-Path "tui-ts")) { Die "tui-ts/ directory not found - are you in the repo root?" }

if ($Force -or -not (Test-Path "tui-ts/node_modules")) {
    Info "Installing UI dependencies (npm install)"
    Push-Location "tui-ts"
    npm install
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Die "npm install failed." }
} else {
    Ok "UI dependencies already installed (use -Force to reinstall)."
}

Info "Building the terminal UI (npm run build)"
Push-Location "tui-ts"
npm run build
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { Die "UI build failed." }
Ok "Terminal UI built."

Write-Host ""
Write-Host "Done! TigerLiteCode is ready." -ForegroundColor Green
Write-Host ""
Write-Host "Next:"
Write-Host '  1. Set an API key, e.g.  $env:DEEPSEEK_API_TIGER_KEY="sk-..."'
Write-Host "  2. Run  tigerlitecode"
