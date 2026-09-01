$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".tools\python\cpython-3.12-windows-x86_64-none\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python is missing. Run the documented bootstrap first."
}

# The runtime environment intentionally stays small (no numpy/torch): it runs
# the pure-contract half of the suite. The torch/numpy-dependent half runs in
# the simulator venv, which carries PyTorch + sb3-contrib.
$smallOnly = @(
    "tests.test_action_codec_v2",
    "tests.test_bc_dataset_merge",
    "tests.test_convert_traces",
    "tests.test_core",
    "tests.test_screen_fixtures",
    "tests.test_trace_controller",
    "tests.test_trace_validation",
    "tests.test_v2_run_wrapper",
    "tests.test_training"
)
$trainingOnly = @(
    "tests.test_behavior_clone",
    "tests.test_behavior_clone_v2",
    "tests.test_dagger_batch",
    "tests.test_prefix_replay_teacher",
    "tests.test_teacher_batch",
    "tests.test_teacher_bc_dataset",
    "tests.test_teacher_v3",
    "tests.test_training",
    "tests.test_v2_contract"
)

Push-Location $projectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"

    Write-Host "== small runtime python: contract tests =="
    & $pythonPath -m unittest @smallOnly -v
    if ($LASTEXITCODE -ne 0) { throw "Contract tests failed with exit code $LASTEXITCODE" }

    $trainingPython = $env:STS2_TRAINING_PYTHON
    if (-not $trainingPython) {
        $trainingPython = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
    }
    if (Test-Path -LiteralPath $trainingPython) {
        Write-Host "== training venv: torch/numpy tests =="
        & $trainingPython -m unittest @trainingOnly -v
        if ($LASTEXITCODE -ne 0) { throw "Torch-side tests failed with exit code $LASTEXITCODE" }
    }
    else {
        Write-Host "Torch-side tests skipped: set STS2_TRAINING_PYTHON to a Python environment with PyTorch."
    }
}
finally {
    Pop-Location
}
