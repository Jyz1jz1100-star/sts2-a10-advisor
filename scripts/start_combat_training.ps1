$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
$supervisorDir = Join-Path $projectRoot "runs\supervised-combat"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Simulator Python is missing: $python"
}

Set-Location -LiteralPath $projectRoot
& $python -m training.supervisor `
    --run-dir $supervisorDir `
    --game-build "24724944/public-beta-v0.111.0/222455745" `
    --character IRONCLAD `
    --ascension 10 `
    -- `
    $python -m training.curriculum `
    --config config\training.toml `
    --only-stage combat
exit $LASTEXITCODE
