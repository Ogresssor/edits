$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:GOCACHE = Join-Path $projectRoot '.local\go-build-cache'
$env:GOPATH = Join-Path $projectRoot '.local\gopath'
$goPath = Join-Path $projectRoot '.local\toolchains\verified\go\bin\go.exe'
if (-not (Test-Path -LiteralPath $goPath)) {
    $goCommand = Get-Command go -ErrorAction SilentlyContinue
    if (-not $goCommand) { throw 'Install Go or run scripts/install-go.ps1' }
    $goPath = $goCommand.Source
}
& $goPath @args
exit $LASTEXITCODE
