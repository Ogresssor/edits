param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$protected = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
function Protect([string]$p) { if ($p) { [void]$protected.Add([IO.Path]::GetFullPath($p)) } }
Get-ChildItem -LiteralPath (Join-Path $root 'review') -Recurse -Filter latest.json -File | ForEach-Object {
    $j = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Protect (Join-Path $_.DirectoryName $j.video)
}
Get-ChildItem -LiteralPath (Join-Path $root 'review') -Recurse -Filter accepted.json -File | ForEach-Object {
    $j = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($j.video) { Protect (Join-Path $_.DirectoryName $j.video) }
}
Get-ChildItem -LiteralPath (Join-Path $root 'review') -Directory -Filter 'youtube*' | ForEach-Object {
    $m = Join-Path $_.FullName 'manifest.json'
    if (Test-Path -LiteralPath $m) {
        $j = Get-Content -LiteralPath $m -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($v in $j.videos) { if ($v.file) { Protect $v.file } }
    }
}
$targets = [Collections.Generic.List[string]]::new()
foreach ($name in @('library','work')) {
    Get-ChildItem -LiteralPath (Join-Path $root $name) -Force | ForEach-Object { $targets.Add($_.FullName) }
}
foreach ($name in @('review','output')) {
    Get-ChildItem -LiteralPath (Join-Path $root $name) -Recurse -Directory | Where-Object {
        $_.Name -eq 'media' -or $_.Name -like '*_media' -or $_.Name -like 'Журналы *'
    } | ForEach-Object { $targets.Add($_.FullName) }
}
Get-ChildItem -LiteralPath (Join-Path $root 'review') -Recurse -File | Where-Object {
    $_.Extension -in @('.mp4','.mov','.wav') -and
    $_.FullName -notmatch '\\FINAL\\' -and -not $protected.Contains($_.FullName)
} | ForEach-Object { $targets.Add($_.FullName) }
foreach ($name in @('editbot','tools','work','review','__pycache__')) {
    $p = Join-Path $root $name
    if ($name -eq '__pycache__' -and (Test-Path -LiteralPath $p)) { $targets.Add($p) }
    elseif (Test-Path -LiteralPath $p) {
        Get-ChildItem -LiteralPath $p -Recurse -Directory -Filter __pycache__ | ForEach-Object { $targets.Add($_.FullName) }
    }
}
$chosen = [Collections.Generic.List[string]]::new()
foreach ($p in ($targets | Sort-Object Length)) {
    $absolute = [IO.Path]::GetFullPath($p)
    if (-not $absolute.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Target outside workspace' }
    if (-not (Test-Path -LiteralPath $absolute)) { continue }
    $item = Get-Item -LiteralPath $absolute -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $absolute" }
    if ($item.PSIsContainer) {
        $links = Get-ChildItem -LiteralPath $absolute -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }
        if ($links) { throw "Nested reparse point: $absolute" }
    }
    if ($protected.Contains($absolute)) { throw 'Protected final selected' }
    foreach ($keep in $protected) { if ($keep.StartsWith($absolute+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Protected final inside directory' } }
    $covered = $false
    foreach ($parent in $chosen) { if ($absolute -eq $parent -or $absolute.StartsWith($parent+'\',[StringComparison]::OrdinalIgnoreCase)) { $covered=$true; break } }
    if (-not $covered) { $chosen.Add($absolute) }
}
$entries = @($chosen | ForEach-Object {
    $item=Get-Item -LiteralPath $_ -Force
    $bytes=if ($item.PSIsContainer) { (Get-ChildItem -LiteralPath $_ -Recurse -File | Measure-Object Length -Sum).Sum } else { $item.Length }
    [PSCustomObject]@{path=$_;bytes=[long]$bytes}
})
$reportDir=Join-Path $root 'review/cleanup_2026-11-09'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$report=[PSCustomObject]@{applied=[bool]$Apply;created=(Get-Date -Format o);targets=$entries;protected=@($protected)}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $reportDir 'manifest.json') -Encoding UTF8
Write-Output ("Targets: {0}; selected GiB: {1:N2}; protected finals: {2}" -f $entries.Count,(($entries | Measure-Object bytes -Sum).Sum/1GB),$protected.Count)
if ($Apply) {
    foreach ($entry in $entries) { Remove-Item -LiteralPath $entry.path -Recurse -Force }
    foreach ($p in $protected) { if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "Missing preserved final: $p" } }
    foreach ($entry in $entries) { if (Test-Path -LiteralPath $entry.path) { throw 'Target still exists' } }
    Write-Output 'Cleanup complete; preserved finals verified.'
}
