# Registers the Spire Oracle supervisor to run at Windows login by creating a
# shortcut in the current user's Startup folder. No admin rights required.
#
#   powershell -ExecutionPolicy Bypass -File autostart\install_autostart.ps1
#
# Uninstall with autostart\uninstall_autostart.ps1

$ErrorActionPreference = "Stop"

# Project root = parent of this script's folder.
$root = Split-Path -Parent $PSScriptRoot
$supervisor = Join-Path $root "autostart\supervisor.py"

# Find pythonw.exe (no console window) next to python.exe.
$python = (Get-Command python -ErrorAction Stop).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "SpireOracle.lnk"

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath = $pythonw
$sc.Arguments = "`"$supervisor`""
$sc.WorkingDirectory = $root
$sc.WindowStyle = 7          # minimized / hidden (pythonw has no window anyway)
$sc.Description = "Spire Oracle autostart supervisor for Slay the Spire 2"
$sc.Save()

Write-Host "Installed autostart shortcut:" -ForegroundColor Green
Write-Host "  $lnkPath"
Write-Host "  -> $pythonw `"$supervisor`""
Write-Host ""
Write-Host "It will start at your next login. To start it now without rebooting:"
Write-Host "  Start-Process `"$pythonw`" -ArgumentList `"$supervisor`" -WorkingDirectory `"$root`""
