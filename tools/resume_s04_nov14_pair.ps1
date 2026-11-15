$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$jobPath = Join-Path $PSScriptRoot 'upload_s04_nov14_pair.py'
$jobLog = Join-Path $projectRoot 'review\youtube_s04_nov14_pair\resume.log'
$jobErrorLog = Join-Path $projectRoot 'review\youtube_s04_nov14_pair\resume.stderr.log'
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-Process -FilePath $pythonPath -ArgumentList @(('"' + $jobPath + '"'), 'run') -WorkingDirectory $projectRoot -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $jobLog -RedirectStandardError $jobErrorLog
exit $process.ExitCode
