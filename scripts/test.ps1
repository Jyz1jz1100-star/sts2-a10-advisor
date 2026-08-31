$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".tools\python\cpython-3.12-windows-x86_64-none\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python is missing. Run the documented bootstrap first."
}

Push-Location $projectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"
    & $pythonPath -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed with exit code $LASTEXITCODE" }

    # The runtime environment intentionally stays small. Re-run the behavior-clone
    # CPU test with the existing simulator/training environment when it is present.
    $trainingPython = $env:STS2_TRAINING_PYTHON
    if (-not $trainingPython) {
        $trainingPython = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
    }
    if (Test-Path -LiteralPath $trainingPython) {
        & $trainingPython -m unittest tests.test_behavior_clone -v
        if ($LASTEXITCODE -ne 0) { throw "Behavior-clone tests failed with exit code $LASTEXITCODE" }
    }
    else {
        Write-Host "Behavior-clone tests skipped: set STS2_TRAINING_PYTHON to a Python environment with PyTorch."
    }
}
finally {
    Pop-Location
}
