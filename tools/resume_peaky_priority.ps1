$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$jobPath = Join-Path $PSScriptRoot 'prioritize_peaky_backlog.py'
$jobLog = Join-Path $projectRoot 'review\youtube_peaky_priority_2026-11-11\scheduled_resume.log'
$env:PYTHONIOENCODING = 'utf-8'
$jobErrorLog = Join-Path $projectRoot 'review\youtube_peaky_priority_2026-11-11\scheduled_resume.stderr.log'
$process = Start-Process -FilePath $pythonPath -ArgumentList @(('"' + $jobPath + '"'), 'run') -WorkingDirectory $projectRoot -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $jobLog -RedirectStandardError $jobErrorLog
exit $process.ExitCode
