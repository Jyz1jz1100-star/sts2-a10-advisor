$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".tools\python\cpython-3.12-windows-x86_64-none\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python is missing. Run the documented bootstrap first."
}

# The runtime environment intentionally stays small (no numpy/torch): it runs
# the pure-contract half of the suite. The numpy/torch/Gymnasium-dependent
# half runs in the simulator venv, which carries the versions from
# requirements-training.txt. For one-environment full discovery, install
# requirements-test.txt and run `python -m unittest discover -s tests`.
$smallOnly = @(
    "tests.test_action_codec_v2",
    "tests.test_autoplay",
    "tests.test_bc_dataset_merge",
    "tests.test_bridge_client",
    "tests.test_bridge_outcome",
    "tests.test_campaign_environment_versions",
    "tests.test_checkpoint_series_guards",
    "tests.test_combat_solver_batch",
    "tests.test_combat_solver_compare",
    "tests.test_combat_solver_contract",
    "tests.test_combat_solver_grammar_v2",
    "tests.test_combat_solver_states",
    "tests.test_convert_traces",
    "tests.test_evidence_manifest",
    "tests.test_event_upgrade_parser",
    "tests.test_emulator_provenance",
    "tests.test_compare_route_policies",
    "tests.test_route_planner",
    "tests.test_core",
    "tests.test_acceptance_protocol",
    "tests.test_deploy_evidence",
    "tests.test_fullauto_keeper",
    "tests.test_full_run_acceptance",
    "tests.test_full_run_contract",
    "tests.test_launch_bulk_act1",
    "tests.test_live_candidate_codec",
    "tests.test_live_choice_policy",
    "tests.test_mod_attestation",
    "tests.test_mod_gate",
    "tests.test_policy_live",
    "tests.test_promotion_gate_absent_inputs",
    "tests.test_report_claim_gate",
    "tests.test_route_executor",
    "tests.test_run_coverage",
    "tests.test_screen_fixtures",
    "tests.test_seed_allocation",
    "tests.test_stage_sts2mcp",
    "tests.test_solver_supervisor",
    "tests.test_trace_controller",
    "tests.test_trace_validation",
    "tests.test_training"
)
$trainingOnly = @(
    "tests.test_behavior_clone",
    "tests.test_behavior_clone_v2",
    "tests.test_dagger_batch",
    "tests.test_dead_end_reward_rule_instruments",
    "tests.test_prefix_replay_teacher",
    "tests.test_teacher_batch",
    "tests.test_teacher_bc_dataset",
    "tests.test_teacher_v3",
    "tests.test_train_seed_lists",
    "tests.test_v2_contract",
    "tests.test_v2_run_wrapper"
)

Push-Location $projectRoot
try {
    $env:PYTHONDONTWRITEBYTECODE = "1"

    Write-Host "== small runtime python: contract tests =="
    & $pythonPath -m unittest @smallOnly -v
    if ($LASTEXITCODE -ne 0) { throw "Contract tests failed with exit code $LASTEXITCODE" }

    $trainingPythonWasConfigured = -not [string]::IsNullOrWhiteSpace($env:STS2_TRAINING_PYTHON)
    $trainingPython = $env:STS2_TRAINING_PYTHON
    if (-not $trainingPythonWasConfigured) {
        $trainingPython = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
    }
    if (Test-Path -LiteralPath $trainingPython) {
        # Keep the failure at the environment boundary.  Without this probe,
        # a missing optional package produces a misleading cascade of import
        # errors from individual training test modules.
        & $trainingPython -c "import gymnasium, numpy, torch, stable_baselines3, sb3_contrib"
        if ($LASTEXITCODE -ne 0) {
            throw "Training tests require requirements-training.txt (gymnasium, numpy, torch, stable-baselines3, sb3-contrib). Set STS2_TRAINING_PYTHON to a compatible interpreter or sync the adjacent emulator environment."
        }
        Write-Host "== training venv: torch/numpy tests =="
        & $trainingPython -m unittest @trainingOnly -v
        if ($LASTEXITCODE -ne 0) { throw "Torch-side tests failed with exit code $LASTEXITCODE" }
    }
    else {
        if ($trainingPythonWasConfigured) {
            throw "STS2_TRAINING_PYTHON does not point to an existing Python interpreter: $trainingPython"
        }
        Write-Host "Training tests skipped: the adjacent emulator .venv is absent. Set STS2_TRAINING_PYTHON to a Python environment installed from requirements-training.txt."
    }
}
finally {
    Pop-Location
}
