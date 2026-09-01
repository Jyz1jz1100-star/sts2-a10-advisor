# Launch the V2 teacher batch-0 generation as ten parallel shards and wait.
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot "..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Simulator Python is missing: $python" }

$outDir = Join-Path $projectRoot "data\teacher\batch0"
New-Item -ItemType Directory -Force $outDir | Out-Null
$shardCount = 10
$seedCount = 4500
$seedStart = 1400100000

$jobs = foreach ($index in 0..($shardCount - 1)) {
    Start-Process -FilePath $python `
        -ArgumentList @(
            "scripts\generate_teacher_batch.py",
            "--seed-count", "$seedCount",
            "--seed-start", "$seedStart",
            "--shard-count", "$shardCount",
            "--shard-index", "$index",
            "--out", "data\teacher\batch0\v2_batch0.jsonl"
        ) `
        -WorkingDirectory $projectRoot -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $outDir "shard$index.log") `
        -RedirectStandardError (Join-Path $outDir "shard$index.err")
}
Write-Output ("launched shard pids: " + ($jobs.Id -join ","))
Wait-Process -Id $jobs.Id
$manifests = @(Get-ChildItem $outDir -Filter "v2_batch0.jsonl.shard*.manifest.json")
$total = 0
foreach ($manifest in $manifests) {
    $payload = Get-Content $manifest.FullName -Raw | ConvertFrom-Json
    $total += [int]$payload.records
    Write-Output ("{0}: records={1} audit={2}" -f $manifest.Name, $payload.records,
        $payload.replay_audit.all_capture_reverify_match)
}
Write-Output ("teacher batch0 complete: {0} manifests, {1} records" -f $manifests.Count, $total)
