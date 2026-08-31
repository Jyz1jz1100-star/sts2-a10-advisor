# Stop the advisor (bridge, overlay, and the autostart supervisor if running).
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'bridge\.main|overlay\\overlay\.py|supervisor\.py' } |
  ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId)"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Write-Host "Spire Oracle stopped." -ForegroundColor Green
