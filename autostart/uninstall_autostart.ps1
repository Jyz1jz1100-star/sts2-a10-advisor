# Removes the Spire Oracle autostart shortcut and stops a running supervisor.
#
#   powershell -ExecutionPolicy Bypass -File autostart\uninstall_autostart.ps1

$ErrorActionPreference = "SilentlyContinue"

$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "SpireOracle.lnk"
if (Test-Path $lnkPath) {
    Remove-Item $lnkPath -Force
    Write-Host "Removed $lnkPath" -ForegroundColor Green
} else {
    Write-Host "No autostart shortcut found."
}

# Stop a running supervisor + its children (pythonw running supervisor.py).
$procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match "supervisor\.py|bridge\.main|overlay\.py" }
foreach ($p in $procs) {
    Write-Host "Stopping PID $($p.ProcessId): $($p.CommandLine)"
    Stop-Process -Id $p.ProcessId -Force
}
Write-Host "Done."
