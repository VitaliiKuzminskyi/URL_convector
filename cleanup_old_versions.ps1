# ============================================================
#  Cleanup of obsolete build artifacts
# ============================================================
#  Removes files for old versions (v1.2.2, v1.3.0, v1.4.0)
#  that are no longer needed. Keeps the current v1.4.1.
#  Asks for confirmation before deleting anything.
# ============================================================

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$targets = @(
    "$root\DESKTOP_version\cURL-to_GET-converter_1.2.2.exe",
    "$root\DESKTOP_version\cURL-to_GET-converter_1.2.2.spec",
    "$root\DESKTOP_version\cURL-to_GET-converter_1.3.0.exe",
    "$root\DESKTOP_version\cURL-to_GET-converter_1.3.0.spec",
    "$root\DESKTOP_version\cURL-to_GET-converter_1.4.0.exe",
    "$root\DESKTOP_version\cURL-to_GET-converter_1.4.0.spec",
    "$root\DESKTOP_version\build_and_cleanup_v1.3.0.bat",
    "$root\DESKTOP_version\build_and_cleanup_v1.3.0.ps1",
    "$root\DESKTOP_version\build_and_cleanup_v1.4.0.bat",
    "$root\DESKTOP_version\build_and_cleanup_v1.4.0.ps1",
    "$root\WEB_version\cURL-to_GET-converter_1.2.2.html",
    "$root\WEB_version\cURL-to_GET-converter_1.3.0.html"
)

Write-Host ''
Write-Host '=== Obsolete files to remove (keeping v1.4.1) ===' -ForegroundColor Cyan
$existing = @()
$totalBytes = 0
foreach ($f in $targets) {
    if (Test-Path $f) {
        $info = Get-Item $f
        if ($info.Length -ge 1MB) {
            $size = ('{0,8:N1} MB' -f ($info.Length / 1MB))
        } else {
            $size = ('{0,8:N1} KB' -f ($info.Length / 1KB))
        }
        Write-Host ("  {0}   {1}" -f $size, $f)
        $existing += $f
        $totalBytes += $info.Length
    }
}

if ($existing.Count -eq 0) {
    Write-Host ''
    Write-Host 'Nothing to clean - already tidy.' -ForegroundColor Green
    Write-Host ''
    Read-Host 'Press Enter to close'
    exit
}

$totalMB = [Math]::Round($totalBytes / 1MB, 1)
Write-Host ''
Write-Host ("Total: {0} files, {1} MB" -f $existing.Count, $totalMB) -ForegroundColor Yellow
Write-Host ''
$resp = Read-Host 'Delete these files? (y/N)'
if ($resp -eq 'y' -or $resp -eq 'Y') {
    foreach ($f in $existing) {
        Remove-Item -Force $f
        Write-Host "  removed  $f" -ForegroundColor Green
    }
    Write-Host ''
    Write-Host '=== Done. ===' -ForegroundColor Green
} else {
    Write-Host 'Cancelled - nothing deleted.' -ForegroundColor Yellow
}
Write-Host ''
Read-Host 'Press Enter to close'
