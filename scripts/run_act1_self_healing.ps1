<#
Self-healing supervisor loop for the act1 stage.

Wraps the reproducible supervisor + curriculum with crash recovery for the
native emulator's sporadic FailFast (the 2026-09-01 07:32 crash: access
violation inside RunMapGenerator.FindAllPaths during a training rollout,
which Python cannot catch). On such a crash the loop resumes the SAME
curriculum run directory from its latest checkpoint
(training.curriculum --resume-run), so lost work is bounded by the 2M
checkpoint interval.

Exit rules:
- promotion_decision.json appears            -> stage settled, loop ends
- child exit 0                               -> ended (settlement or budget)
- non-crash (e.g. SystemExit promotion fail) -> no auto-resume, end
- crash within 12 min AND no new checkpoint
  twice in a row                             -> crash-storm guard, end
- otherwise                                  -> resume
#>

param(
    [int]$MaxRestarts = 12,
    [string]$CurriculumRun = "runs\curriculum\curriculum-20260831T205351Z"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$python = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Simulator Python is missing: $python" }

$absCurriculum = if ([IO.Path]::IsPathRooted($CurriculumRun)) { $CurriculumRun } else { Join-Path $projectRoot $CurriculumRun }
$decisionPath = Join-Path $absCurriculum "act1\promotion_decision.json"

function Test-CrashExit([int]$code) {
    # Native FailFast surfaces as negative signed ints (0xC0000409 => -1073740791,
    # 0xC0000005 => -1073741819); also accept the unsigned 3221225477 form.
    return ($code -lt 0) -or ($code -eq 3221225477)
}

function Get-LatestCheckpoint {
    $dir = Join-Path $absCurriculum "act1\checkpoints"
    if (-not (Test-Path -LiteralPath $dir)) { return "" }
    $best = Get-ChildItem -LiteralPath $dir -Filter 'step_*.zip' | Sort-Object Name | Select-Object -Last 1
    if ($best) { return $best.Name } else { return "" }
}

$noProgress = 0
for ($attempt = 1; $attempt -le ($MaxRestarts + 1); $attempt++) {
    if (Test-Path -LiteralPath $decisionPath) {
        Write-Host "stage already settled before attempt $attempt; ending"
        exit 0
    }
    $before = Get-LatestCheckpoint
    $resumeFlag = @()
    if (Test-Path -LiteralPath $absCurriculum) {
        $resumeFlag = @("--resume-run", $CurriculumRun)
    }
    Write-Host "=== attempt $attempt (resume=$($resumeFlag.Count -gt 0), latest_ckpt=$before) ==="
    $start = Get-Date
    & $python -m training.supervisor `
        --run-dir "runs\supervised-act1" `
        --game-build "24724944/public-beta-v0.111.0/222455745" `
        --character IRONCLAD `
        --ascension 10 `
        -- `
        $python -m training.curriculum `
        --config config\training.toml `
        --only-stage act1 @resumeFlag
    $code = $LASTEXITCODE
    $minutes = [math]::Round(((Get-Date) - $start).TotalMinutes, 1)
    $after = Get-LatestCheckpoint

    if (Test-Path -LiteralPath $decisionPath) {
        Write-Host "stage settled (promotion_decision.json) after $minutes min (child exit $code)"
        exit $code
    }
    if ($code -eq 0) {
        Write-Host "child exited 0 after $minutes min without settlement; inspect run dir"
        exit 0
    }
    if (-not (Test-CrashExit $code)) {
        Write-Host "non-crash failure (exit $code after $minutes min); NOT auto-resuming"
        exit $code
    }
    if ($after -eq $before -and $minutes -lt 12) {
        $noProgress++
        Write-Host "crash (exit $code) after $minutes min with no new checkpoint (noProgress=$noProgress)"
        if ($noProgress -ge 2) {
            Write-Host "crash-storm guard tripped; manual inspection needed"
            exit $code
        }
    } else {
        $noProgress = 0
        Write-Host "crash (exit $code) after $minutes min; checkpoint moved: '$before' -> '$after'"
    }
    Write-Host "resuming from latest checkpoint..."
}
Write-Host "MaxRestarts ($MaxRestarts) exhausted without settlement"
exit 1
