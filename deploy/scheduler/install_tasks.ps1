<#
    Register the ADB bot's scheduled loops in Windows Task Scheduler.

    Run once on the server (from an elevated PowerShell if you want the tasks to
    run whether or not the user is logged on). Re-running is safe -- each task is
    registered with -Force, so it updates in place.

    Frequencies follow BOT_RESPONSIBILITIES_CHECKLIST.md:
      posting   every 10 min   (slots are fixed; just catch each one)
      pipeline  every 20 min   (same-day spoofing)
      warmup    3x/day         (08:00 / 13:00 / 19:00 local)
      mlx-sync  daily          (23:30 local)

    Usage:
      .\install_tasks.ps1                 # register in DRY-RUN (safe; plans only)
      .\install_tasks.ps1 -Apply          # register the loops to do real work
      .\install_tasks.ps1 -Remove         # unregister all four tasks
#>

param(
    # Repo root = two levels up from this script (deploy\scheduler\install_tasks.ps1).
    [string]$RepoRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),
    [string]$Python = "",
    [switch]$Apply,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$prefix = "ADBBot-"
$loops = @("posting", "warmup", "pipeline", "mlx-sync")

if ($Remove) {
    foreach ($loop in $loops) {
        $name = "$prefix$loop"
        try {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
            Write-Host "Removed $name"
        } catch {
            Write-Host "  (no task $name)"
        }
    }
    return
}

if (-not $Python) { $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "Python not found at $Python. Pass -Python <path\to\python.exe>." }

$applyArg = ""
if ($Apply) { $applyArg = " --apply" }
$mode = if ($Apply) { "APPLY" } else { "DRY-RUN" }
Write-Host "Registering ADB bot tasks in $mode mode"
Write-Host "  RepoRoot: $RepoRoot"
Write-Host "  Python:   $Python"

# Repeat "forever" -- Task Scheduler rejects TimeSpan.MaxValue, so use a very long span.
$forever = New-TimeSpan -Days 3650

function Register-Loop([string]$loop, $trigger, [string]$desc) {
    $name = "$prefix$loop"
    $action = New-ScheduledTaskAction -Execute $Python `
        -Argument "-m adb_bot.automation.run_loop $loop$applyArg" `
        -WorkingDirectory $RepoRoot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description $desc -Force | Out-Null
    Write-Host "  registered $name"
}

# posting: every 10 min, all day.
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
$t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration $forever).Repetition
Register-Loop "posting" $t "ADB bot posting loop (Posting Queue -> IG)"

# pipeline: every 20 min, all day.
$t = New-ScheduledTaskTrigger -Once -At (Get-Date).Date
$t.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration $forever).Repetition
Register-Loop "pipeline" $t "ADB bot spoofing pipeline (Drive/raw -> Spoof Variants)"

# warmup: 3x/day.
$warmupTriggers = @(
    (New-ScheduledTaskTrigger -Daily -At 8am),
    (New-ScheduledTaskTrigger -Daily -At 1pm),
    (New-ScheduledTaskTrigger -Daily -At 7pm)
)
Register-Loop "warmup" $warmupTriggers "ADB bot warmup loop (lifecycle Day 1-4)"

# mlx-sync: daily late.
Register-Loop "mlx-sync" (New-ScheduledTaskTrigger -Daily -At 11:30pm) "ADB bot MultiLogin->Airtable profile sync"

Write-Host ""
Write-Host "Done. Review in Task Scheduler (taskschd.msc) under the '$prefix*' names."
Write-Host "Tokens: set MULTILOGIN_TOKEN / AIRTABLE_TOKEN as machine env vars, or save them in dev settings, before the tasks run."
if (-not $Apply) {
    Write-Host "These are DRY-RUN tasks (plan only). Re-run with -Apply once you've verified the plans in the logs."
}
