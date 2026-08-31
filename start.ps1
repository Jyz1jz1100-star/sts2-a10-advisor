# Launch the advisor manually (bridge + overlay), no console windows.
# Use this if you skipped auto-launch during setup.
$root = $PSScriptRoot
Set-Location $root
Start-Process pythonw -ArgumentList "-m bridge.main" -WorkingDirectory $root
Start-Process pythonw -ArgumentList ('"' + (Join-Path $root "overlay\overlay.py") + '"') -WorkingDirectory $root
Write-Host "Spire Oracle started (bridge + overlay). Launch StS2 and play!" -ForegroundColor Green
Write-Host "To stop it, run stop.bat." -ForegroundColor Green
