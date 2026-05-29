# ============================================================
#  cURL-to_GET-converter  v1.4.1  —  build + cleanup
# ============================================================
#  Run this on WINDOWS (double-click build_and_cleanup_v1.4.1.bat,
#  or right-click this .ps1 -> Run with PowerShell).
#
#  What it does:
#    1. builds  cURL-to_GET-converter_1.4.1.exe  with PyInstaller
#    2. moves the .exe next to main.py
#    3. removes the build artifacts (build/, dist/, __pycache__/)
# ============================================================

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

Write-Host ''
Write-Host '=== cURL-to_GET-converter v1.4.1 — build ===' -ForegroundColor Cyan

# --- pick Python: prefer the project virtual environment ---
$venvPy = Join-Path $root '..\.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    $py = $venvPy
    Write-Host 'Python : project .venv'
} else {
    $py = 'python'
    Write-Host 'Python : system PATH'
}

# --- make sure PyInstaller is available ---
& $py -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'PyInstaller not found - installing it...' -ForegroundColor Yellow
    & $py -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install PyInstaller.' }
}

# --- build ---
Write-Host 'Building .exe (this can take a minute)...' -ForegroundColor Cyan
& $py -m PyInstaller --clean --noconfirm 'cURL-to_GET-converter_1.4.1.spec'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

# --- move the produced .exe next to main.py ---
$built  = Join-Path $root 'dist\cURL-to_GET-converter_1.4.1.exe'
$target = Join-Path $root 'cURL-to_GET-converter_1.4.1.exe'
if (-not (Test-Path $built)) { throw "Built .exe not found: $built" }
Move-Item -Force $built $target
Write-Host "Built  : $target" -ForegroundColor Green

# --- cleanup build artifacts ---
Write-Host 'Cleaning up build artifacts...' -ForegroundColor Cyan
foreach ($d in @('build', 'dist', '__pycache__')) {
    $p = Join-Path $root $d
    if (Test-Path $p) {
        Remove-Item -Recurse -Force $p
        Write-Host "  removed  $d"
    }
}

Write-Host ''
Write-Host '=== Done. v1.4.1 .exe is ready. ===' -ForegroundColor Green
Write-Host ''
Read-Host 'Press Enter to close'
