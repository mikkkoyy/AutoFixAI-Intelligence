<#
.SYNOPSIS
    Builds AutoFix AI Studio Launcher into a single-file Windows .exe
    using PyInstaller.

.DESCRIPTION
    Activates the root venv, installs PyInstaller if needed, and runs
    PyInstaller to produce a single-file console executable.

    The resulting .exe lives in  launcher/dist/launcher.exe

.PARAMETER Clean
    Remove previous build artifacts before building.

.EXAMPLE
    .\build_launcher.ps1
    .\build_launcher.ps1 -Clean
#>

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$RootVenv = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $RootVenv "Scripts\python.exe"

# ── Sanity checks ──────────────────────────────────────────────────────

if (-not (Test-Path $Python)) {
    Write-Error "Root venv not found at $RootVenv. Run: python -m venv .venv && .\.venv\Scripts\Activate.ps1 && pip install -r requirements.txt"
    exit 1
}

# ── Clean old artifacts ────────────────────────────────────────────────

$BuildDir  = Join-Path $ScriptDir "build"
$DistDir   = Join-Path $ScriptDir "dist"
$SpecFile  = Join-Path $ScriptDir "launcher.spec"

if ($Clean) {
    Write-Host "Cleaning previous build artifacts..." -ForegroundColor Yellow
    foreach ($d in @($BuildDir, $DistDir)) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force }
    }
    if (Test-Path $SpecFile) { Remove-Item $SpecFile -Force }
}

# ── Ensure PyInstaller is available ────────────────────────────────────

& $Python -m PyInstaller --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyInstaller..." -ForegroundColor Cyan
    & $Python -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install PyInstaller"
        exit 1
    }
}

# ── Activate root venv ────────────────────────────────────────────────

& (Join-Path $RootVenv "Scripts\Activate.ps1")

# ── Run PyInstaller ───────────────────────────────────────────────────

$LauncherScript = Join-Path $ScriptDir "launcher.py"

Write-Host ""
Write-Host "Building AutoFix AI Studio Launcher..." -ForegroundColor Green
Write-Host "  Script:  $LauncherScript"
Write-Host "  Output:  $DistDir\launcher.exe"
Write-Host ""

pyinstaller `
    --onefile `
    --windowed `
    --name "launcher" `
    --distpath  $DistDir `
    --workpath  $BuildDir `
    --specpath  $ScriptDir `
    --noconfirm `
    --clean `
    $LauncherScript

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller build failed"
    exit 1
}

# ── Post-build summary ────────────────────────────────────────────────

$ExePath = Join-Path $DistDir "launcher.exe"

if (Test-Path $ExePath) {
    $SizeMB = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Build succeeded!" -ForegroundColor Green
    Write-Host "  EXE:  $ExePath"
    Write-Host "  Size: $SizeMB MB"
    Write-Host ""
    Write-Host "Usage:" -ForegroundColor Cyan
    Write-Host "  1. Place the .exe in the project root (one level above backend/)"
    Write-Host "  2. Double-click or run: .\dist\launcher.exe"
    Write-Host "  3. Logs are written to: logs\launcher.log"
} else {
    Write-Error "Build completed but launcher.exe not found"
    exit 1
}
