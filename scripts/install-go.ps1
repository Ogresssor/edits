$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolchainRoot = Join-Path $projectRoot '.local\toolchains'
$destination = Join-Path $toolchainRoot 'verified'
if (Test-Path -LiteralPath (Join-Path $destination 'go\bin\go.exe')) {
    & (Join-Path $destination 'go\bin\go.exe') version
    exit $LASTEXITCODE
}
if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne 'X64') { throw 'This installer is for Windows amd64 only' }
New-Item -ItemType Directory -Path $toolchainRoot -Force | Out-Null
$release = Invoke-RestMethod -Uri 'https://go.dev/dl/?mode=json' -TimeoutSec 30 | Where-Object {$_.stable} | Select-Object -First 1
$archive = $release.files | Where-Object {$_.os -eq 'windows' -and $_.arch -eq 'amd64' -and $_.kind -eq 'archive'} | Select-Object -First 1
if (-not $archive) { throw 'Official Go archive not found' }
$archivePath = Join-Path $toolchainRoot 'go-verified.zip'
& curl.exe --fail --location --silent --show-error --max-time 180 ('https://dl.google.com/go/' + $archive.filename) --output $archivePath
if ($LASTEXITCODE -ne 0) { throw 'Go download failed' }
if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $archive.sha256) { throw 'Go archive checksum mismatch' }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($archivePath,$destination)
& (Join-Path $destination 'go\bin\go.exe') version
