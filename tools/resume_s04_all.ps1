$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$jobPath = Join-Path $PSScriptRoot 'upload_s04_all.py'
$jobLog = Join-Path $projectRoot 'review\youtube_s04_all_2026-11-12\resume.log'
$jobErrorLog = Join-Path $projectRoot 'review\youtube_s04_all_2026-11-12\resume.stderr.log'
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-Process -FilePath $pythonPath -ArgumentList @(('"' + $jobPath + '"'), 'run') -WorkingDirectory $projectRoot -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $jobLog -RedirectStandardError $jobErrorLog
if ($process.ExitCode -eq 0) {
    Disable-ScheduledTask -TaskName 'KinoMoment-S04-All-Approved-20261112' | Out-Null
}
exit $process.ExitCode
